# View Skill

View the full detail of a skill, including its prompt template, steps, and preconditions.

1. Call `registry__get_skill` with `skill_id="$ARGUMENTS"` to retrieve the skill metadata (agent_role, version, status, steps, preconditions).
2. Call `registry__get_skill_prompt` with `skill_id="$ARGUMENTS"` to retrieve the full prompt template.

Display both results together. If the skill is revoked or expired, `registry__get_skill_prompt` will return a 410 error — note that in the output.
