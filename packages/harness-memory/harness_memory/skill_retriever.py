def retrieve_skill(store, agent_role: str, task: str) -> dict:
    """Find ACTIVE, non-stale skill formulas matching an agent role and task.

    Calls store.list_matches(agent_role, task) and formats a ranked list of
    matches, each carrying its id and match score. Returns matched=False with
    an empty matches list when no formula scores above threshold.
    """
    matches = store.list_matches(agent_role, task)
    return {
        "matches": [
            {
                "id": formula.id,
                "name": formula.name,
                "description": formula.description,
                "steps": formula.steps,
                "input_schema": formula.input_schema,
                "output_contract": formula.output_contract,
                "score": score,
            }
            for formula, score in matches
        ],
        "matched": bool(matches),
        "query": task,
    }
