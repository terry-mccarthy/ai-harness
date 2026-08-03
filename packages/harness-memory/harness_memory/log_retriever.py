# Relevance threshold for a log match, in cosine-similarity score terms.
#
# Log entries are embedded as their full JSON blob (log_seed.py writes
# `{**entry, "id": key}` with no curated `_embedding_text` override), so the
# embedded text is noisier than a runbook's hand-written "When to use"
# signature — timestamps, trace IDs, and field names dilute the semantic
# signal relative to curated prose. There's no established "log relevance"
# convention yet (unlike runbooks' RUNBOOK_MIN_SCORE = 0.80, which mirrors
# consolidation.py's CLUSTER_THRESHOLD), so this is a considered starting
# point rather than a measured one: low enough to tolerate that extra noise
# and still surface genuine matches, high enough to exclude clearly
# unrelated lines. Sits in the middle of the ~0.5-0.7 band that's more
# appropriate for short/noisy log lines than runbooks' curated-text 0.80.
# Revisit once real query/result pairs against docs/logs/*.jsonl are
# available to calibrate against.
LOG_MIN_SCORE = 0.55

# Max log lines shown to the agent per query. Named explicitly (rather than
# an implicit top_k=5 kwarg default) so the cap is discoverable/tunable, the
# same pattern as RUNBOOK_MIN_SCORE.
MAX_LOG_RESULTS = 5

# Internal-only cap on how many min_score-qualifying rows we pull from the
# store before slicing down to MAX_LOG_RESULTS for display. Exists purely so
# total_count reflects "how many rows actually cleared the threshold" rather
# than "how many rows the display cap allowed through" — PostgresMemoryStore
# .search() applies LIMIT in SQL, so a call capped at top_k can never reveal
# how many more rows would have qualified. Not exposed to the MCP tool.
#
# Trade-off: this makes total_count exact for any namespace with <= this many
# qualifying rows, and a floor (undercount) beyond it. Chosen over a second
# COUNT(*) query/store method because it costs one embedding call instead of
# two, needs no changes to PostgresMemoryStore, and docs/logs/*.jsonl today
# has 14 total lines — comfortably inside this cap. If the log corpus grows
# to consistently exceed it, switch to a dedicated count query.
_TOTAL_COUNT_FETCH_CAP = 200


async def retrieve_logs(
    store,
    query: str,
    top_k: int = MAX_LOG_RESULTS,
    min_score: float = LOG_MIN_SCORE,
) -> dict:
    """Search the log store semantically and return formatted results.

    Returns log entries ordered by cosine similarity to *query*, highest
    first, restricted to matches scoring at or above *min_score* and capped
    at *top_k* for display. Score is rounded to 3 decimal places.

    `returned_count` is the number of log entries actually returned (<=
    top_k). `total_count` is the number of rows in the namespace that scored
    at or above *min_score*, independent of the display cap — so callers can
    tell "these are all the relevant lines" from "these are N of total_count,
    truncated". When nothing clears the threshold, both counts are 0 and
    "logs" is an empty list — the same well-formed empty shape as an empty
    store, no separate "no match" error.
    """
    matches = await store.search(
        "logs", query, top_k=max(top_k, _TOTAL_COUNT_FETCH_CAP), min_score=min_score
    )
    results = matches[:top_k]
    return {
        "logs": [
            {
                "id": r["key"],
                "timestamp": r["value"].get("timestamp", ""),
                "level": r["value"].get("level", ""),
                "service": r["value"].get("service", ""),
                "message": r["value"].get("message", ""),
                "score": round(r["score"], 3),
            }
            for r in results
        ],
        "query": query,
        "returned_count": len(results),
        "total_count": len(matches),
    }
