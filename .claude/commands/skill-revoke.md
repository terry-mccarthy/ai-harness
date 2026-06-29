# Revoke Skill

Immediately revoke an active skill (human-operator only). In-flight executions are denied on their next step.

Required from the user (or $ARGUMENTS):
- `skill_id` — UUID or name of the skill to revoke
- `reason` — required revocation rationale (recorded in the Dolt audit trail)

Call `registry__revoke_skill` with `skill_id` and `reason`. Confirm the skill is revoked by showing `status="revoked"` in the response.

After revoking, run `/sync-skills` to remove the stale generated slash command from `.claude/commands/`.
