def retrieve_skill(store, agent_role: str, task: str) -> dict:
    """Find the best matching active skill formula for an agent role and task.

    Calls store.lookup(agent_role, task) and formats the result.
    Returns matched=False with skill=None when no formula scores above threshold.
    """
    formula = store.lookup(agent_role, task)
    if formula is None:
        return {"skill": None, "matched": False, "query": task}
    return {
        "skill": {
            "id": formula.id,
            "name": formula.name,
            "description": formula.description,
            "steps": formula.steps,
            "input_schema": formula.input_schema,
            "output_contract": formula.output_contract,
        },
        "matched": True,
        "query": task,
    }
