"""Consolidation worker: clusters episodic memories and promotes to semantic."""
from __future__ import annotations

import uuid
import numpy as np

from .models import ConsolidationResult

# Cosine similarity threshold for merging two episodic items.
# nomic-embed-text produces clean separation: same-topic pairs ~0.82–0.93,
# different-topic pairs ~0.35–0.62.  0.80 catches near-duplicates while
# keeping genuinely different items apart.
CLUSTER_THRESHOLD = 0.80
# Minimum pours before quality scoring takes effect
MIN_POURS = 10
PROVEN_THRESHOLD = 0.80
REVIEW_THRESHOLD = 0.30


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _greedy_cluster(items: list[dict]) -> list[list[dict]]:
    """Group items into clusters where all pairs have cosine_sim > CLUSTER_THRESHOLD."""
    clusters: list[list[dict]] = []
    assigned = [False] * len(items)

    for i, item in enumerate(items):
        if assigned[i]:
            continue
        cluster = [item]
        assigned[i] = True
        if item["embedding"] is None:
            clusters.append(cluster)
            continue
        for j in range(i + 1, len(items)):
            if assigned[j] or items[j]["embedding"] is None:
                continue
            sim = _cosine_sim(item["embedding"], items[j]["embedding"])
            if sim >= CLUSTER_THRESHOLD:
                cluster.append(items[j])
                assigned[j] = True
        clusters.append(cluster)

    return clusters


class ConsolidationWorker:
    def __init__(self, store, formula_store) -> None:
        self._store = store
        self._formula_store = formula_store

    async def run_pass(self, namespace: str) -> ConsolidationResult:
        result = ConsolidationResult()

        # 1. Prune expired items
        result.items_pruned = await self._store._delete_expired(namespace)

        # 2. Load unconsolidated episodic items
        items = await self._store._fetch_unconsolidated_episodic(namespace)
        if not items:
            await self._update_formula_quality(result)
            return result

        # 3. Cluster by cosine similarity
        clusters = _greedy_cluster(items)

        # 4. For each cluster: create a semantic item, mark sources consolidated
        for cluster in clusters:
            semantic_key = f"semantic:{uuid.uuid4().hex[:8]}"
            if len(cluster) == 1:
                semantic_value = cluster[0]["value"]
            else:
                # Merge: combine text fields, or just use first item
                texts = [
                    item["value"].get("text", str(item["value"]))
                    for item in cluster
                ]
                semantic_value = {"text": " | ".join(texts), "source_count": len(cluster)}

            source_ids = [item["id"] for item in cluster]
            await self._store._write_semantic(namespace, semantic_key, semantic_value, source_ids)
            result.semantic_items_created += 1

            for item in cluster:
                await self._store._mark_consolidated(namespace, item["key"])
                result.episodes_consolidated += 1

        # 5. Update formula quality scores
        await self._update_formula_quality(result)

        return result

    async def _update_formula_quality(self, result: ConsolidationResult) -> None:
        formula_ids = self._formula_store.get_all_formula_ids()
        for fid in formula_ids:
            stats = self._formula_store.get_pour_stats(fid)
            total = stats["total"]
            if total < MIN_POURS:
                continue
            rate = stats["successes"] / total
            if rate >= PROVEN_THRESHOLD:
                new_status = "proven"
            elif rate < REVIEW_THRESHOLD:
                new_status = "review"
            else:
                new_status = "active"
            self._formula_store.update_quality(fid, rate, new_status)
            result.formulas_updated += 1
