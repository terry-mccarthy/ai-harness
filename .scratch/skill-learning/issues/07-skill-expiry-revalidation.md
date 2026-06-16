---
title: "Skill expiry and lightweight re-validation trigger"
status: ready-for-agent
type: AFK
---

## What to build

Skills are expiring assets. Add an expiry pass and a re-validation path that feeds expired skills back through the episode → candidate → HITL pipeline.

**Expiry pass** — `POST /skills/expire` on governance (no body required). Operator calls this manually, or it can be wired to a simple counter: governance tracks how many audit events have been processed since the last expiry pass, and auto-triggers it every 1000 calls (configurable via `EXPIRY_PASS_INTERVAL` env var, default 1000). The pass:
1. Finds all `skills` rows where `status = ACTIVE` and `expires_at <= NOW()`.
2. Transitions each to `status = EXPIRED`.
3. Commits to Dolt with message `skill: <id> expired`.
4. Returns a summary `{expired_count: N, skill_ids: [...]}`.

**Re-validation** — for each newly EXPIRED skill, the pass also checks whether enough recent RESOLVED episodes share the same `cluster_key` to re-propose a candidate automatically. If the volume/independence/recency criteria (from issue 04) are met, it creates a new `candidates` row with `status = PROPOSED` pointing at the fresh episode set. The human still must promote (issue 05) — re-validation only surfaces the candidate, it does not promote.

If re-validation produces a candidate, the summary includes `{re_proposed_candidates: [...]}` so the operator knows to review.

**Early review flag** — during the expiry pass, if an ACTIVE skill has a trailing 30-day success rate below 0.5 (computed from `audit_log` entries matching the skill's `cluster_key`), flag it in the response as `{flagged_for_early_review: [...]}` without changing its status.

## Acceptance criteria

- [ ] `POST /skills/expire` transitions all overdue ACTIVE skills to EXPIRED and commits each to Dolt
- [ ] Skills transitioned to EXPIRED are not executable (issue 06 flow returns an error)
- [ ] Re-validation auto-proposes a candidate when sufficient recent RESOLVED episodes exist for the expired skill's cluster_key
- [ ] Auto-trigger fires after `EXPIRY_PASS_INTERVAL` audit events (configurable; tested with interval=3 in integration tests)
- [ ] Early-review flag appears in response for skills with low recent success rate
- [ ] Integration test: create a skill with `expires_at` in the past, call the expire endpoint, verify EXPIRED status and candidate re-proposal

## Blocked by

- [06 — Skill execution with per-step OPA re-check and revocation](06-skill-execution-revocation.md)
