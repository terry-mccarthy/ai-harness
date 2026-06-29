# Promote Skill Candidate

Promote a proposed candidate to an active skill (human-operator only).

Required from the user (or $ARGUMENTS):
- `candidate_id` — UUID of the candidate to promote

Before promoting, optionally call `registry__get_candidate` with `candidate_id` to review its support stats (episode count, success rate, agent_role).

Call `registry__promote_candidate` with `candidate_id`. On success the new `skill_id` is returned. Suggest running `/sync-skills` to generate the corresponding slash command.
