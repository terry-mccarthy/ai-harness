# Reject Skill Candidate

Reject a proposed candidate with a required reason (human-operator only).

Required from the user (or $ARGUMENTS):
- `candidate_id` — UUID of the candidate to reject
- `reason` — required rejection rationale (recorded in the Dolt audit trail)

Call `registry__reject_candidate` with `candidate_id` and `reason`. Confirm the rejection was recorded by showing the returned candidate record with `status="REJECTED"`.
