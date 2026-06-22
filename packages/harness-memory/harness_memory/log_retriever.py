async def retrieve_logs(store, query: str, top_k: int = 5) -> dict:
    """Search the log store semantically and return formatted results.

    Returns log entries ordered by cosine similarity to *query*, highest first.
    Score is rounded to 3 decimal places.
    """
    results = await store.search("logs", query, top_k=top_k)
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
    }
