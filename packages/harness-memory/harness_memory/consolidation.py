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


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _gather_similar(
    item: dict, items: list[dict], i: int, assigned: list[bool], cluster: list[dict]
) -> None:
    for j in range(i + 1, len(items)):
        if assigned[j] or items[j]["embedding"] is None:
            continue
        if _cosine_sim(item["embedding"], items[j]["embedding"]) >= CLUSTER_THRESHOLD:
            cluster.append(items[j])
            assigned[j] = True


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
        _gather_similar(item, items, i, assigned, cluster)
        clusters.append(cluster)

    return clusters


class ConsolidationWorker:
    def __init__(self, store, formula_store) -> None:
        self._store = store
        self._formula_store = formula_store

    async def _cluster_semantic_value(self, cluster: list[dict]) -> dict:
        if len(cluster) == 1:
            return cluster[0]["value"]
        texts = [
            item["value"].get("text", str(item["value"]))
            for item in cluster
        ]
        return {"text": " | ".join(texts), "source_count": len(cluster)}

    async def _process_cluster(self, namespace: str, cluster: list[dict], result: ConsolidationResult) -> None:
        semantic_key = f"semantic:{uuid.uuid4().hex[:8]}"
        semantic_value = await self._cluster_semantic_value(cluster)
        source_ids = [item["id"] for item in cluster]
        await self._store._write_semantic(namespace, semantic_key, semantic_value, source_ids)
        result.semantic_items_created += 1
        for item in cluster:
            await self._store._mark_consolidated(namespace, item["key"])
            result.episodes_consolidated += 1

    async def run_pass(self, namespace: str) -> ConsolidationResult:
        result = ConsolidationResult()

        result.items_pruned = await self._store._delete_expired(namespace)

        items = await self._store._fetch_unconsolidated_episodic(namespace)
        if not items:
            await self._update_formula_quality(result)
            return result

        clusters = _greedy_cluster(items)

        for cluster in clusters:
            await self._process_cluster(namespace, cluster, result)

        return result
