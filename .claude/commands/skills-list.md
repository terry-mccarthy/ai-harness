# List Skills

List all active skills in the registry.

Call `registry__list_skills` with `status_filter="active"` and display the results as a table showing `id`, `name`, `agent_role`, `version`, and `expires_at`.

To also see revoked or expired skills, call with `status_filter="revoked"` or `status_filter="expired"`.
