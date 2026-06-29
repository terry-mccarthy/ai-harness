# Create Skill

Manually author and register a new skill in the registry (human-operator only).

Collect the following from the user (or $ARGUMENTS):
- `skill_name` — short kebab-case name, e.g. "triage-db-latency"
- `agent_role` — one of: sre, code_reviewer, architect
- `description` — one sentence for TF-IDF lookup
- `prompt_template` — full system prompt / instruction block
- `steps` — ordered list of tool calls, each with `action`, `params`, `on_failure` (ABORT|ROLLBACK|CONTINUE)
- `preconditions` (optional) — `env_constraints` dict and `task_patterns` list
- `input_schema` (optional) — JSON Schema for inputs
- `output_contract` (optional) — JSON Schema for expected output

Call `registry__create_skill` with the collected values. On success a `skill_id` is returned — confirm it with the user and suggest running `/sync-skills` to generate the corresponding slash command.
