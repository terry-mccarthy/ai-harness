# Relevance threshold for a runbook match, in cosine-similarity score terms.
# Mirrors the memory layer's existing CLUSTER_THRESHOLD (0.80) convention —
# see harness_memory/consolidation.py. Passed to PostgresMemoryStore.search()
# as min_score so weak/irrelevant matches are filtered out server-side rather
# than surfaced to the agent as a runbook_ref.
RUNBOOK_MIN_SCORE = 0.80


async def retrieve_runbooks(
    store, query: str, top_k: int = 3, min_score: float = RUNBOOK_MIN_SCORE
) -> dict:
    """Search the runbook store semantically and return formatted results.

    Returns runbooks ordered by cosine similarity to *query*, highest first,
    restricted to matches scoring at or above *min_score*. Score is rounded
    to 3 decimal places.

    When no result clears the threshold, "runbooks" is an empty list — the
    same shape already returned for an empty store — so callers (e.g. the
    SRE agent) can null out `runbook_ref` without a separate "no match"
    signal to check for.
    """
    results = await store.search("runbooks", query, top_k=top_k, min_score=min_score)
    return {
        "runbooks": [
            {
                "id": r["key"],
                "signature": r["value"].get("signature", ""),
                "body": r["value"].get("body", ""),
                "score": round(r["score"], 3),
            }
            for r in results
        ],
        "query": query,
    }
