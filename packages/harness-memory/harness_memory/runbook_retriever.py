async def retrieve_runbooks(store, query: str, top_k: int = 3) -> dict:
    """Search the runbook store semantically and return formatted results.

    Returns runbooks ordered by cosine similarity to *query*, highest first.
    Score is rounded to 3 decimal places.
    """
    results = await store.search("runbooks", query, top_k=top_k)
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
