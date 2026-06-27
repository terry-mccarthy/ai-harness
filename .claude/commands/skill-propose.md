# Propose Skill Candidate

Propose a skill candidate from a qualifying set of labeled episodes.

Required from the user (or $ARGUMENTS — comma-separated episode UUIDs):
- `episode_ids` — list of episode UUIDs to base the candidate on

Episodes must be labeled with a positive outcome (RESOLVED) and meet the minimum support threshold configured in governance.

Call `registry__propose_candidate` with `episode_ids`. On success, a `candidate_id` is returned. Share it with the user and suggest running `/skill-promote` once the candidate is ready for review.
