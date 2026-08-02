---
title: "Skill-aware guidance and precedence in the SRE agent"
status: ready-for-agent
type: AFK
---

## Implementation status (audited 2026-08-02, revised 2026-08-03)

**Correction to the first pass:** this was originally logged as `needs-info` —
"decide whether to accept the preload-and-auto-execute mechanism or rebuild
around `run_skill`" — framed as if `run_skill` didn't exist yet and this were
an open design fork, same as issue 02. Digging further shows that's wrong:
`run_skill` **already exists**, fully built, and is already used elsewhere in
this codebase. This isn't a design decision — it's unfinished wiring.

**What already exists:** `GatewayClient.execute_skill(skill_id, inputs)`
(`packages/harness-gateway/harness_gateway/client.py:317`) delegates to
`SkillRunner` (`packages/harness-gateway/harness_gateway/skill_runner.py`),
which fetches the skill from governance (`GET /skills/{id}`, raising on a
410/404 for revoked/missing skills), walks its steps through
`gateway.call_tool()` (so every step still gets the per-step OPA check), and
**honours each step's `on_failure` policy — ABORT / CONTINUE / ROLLBACK**.
`review_server` already exposes this as a real, LLM-callable MCP tool named
`run_skill` (`services/review_server/routers/review.py:49`), which just calls
`gateway.execute_skill()`.

**What the SRE agent actually does instead:**
`DynamicSREAgent._load_formula()` calls `formula_store.lookup()` directly
(bypassing `skill_search`/issue 05 and governance's serving API — it reads
straight from `DoltFormulaStore`), then `_execute_formula_steps()` hand-rolls
its own step loop that duplicates a *weaker* subset of `SkillRunner`: it
catches `ToolAccessDenied` per step but **always continues to the next step
regardless of `on_failure`** — a step declared `on_failure: "ABORT"` is not
aborted. No `expected_signal` check, no revocation check via governance's
skill-serving endpoint (only whatever `DoltFormulaStore.list_active()`'s SQL
filter catches). No `run_skill` tool is registered anywhere for the SRE
role — checked `TOOL_NAME_MAP` in `harness_gateway/client.py`,
`stub_servers/sre_server.py`, and `prompts/react_sre.md`.

The safety invariant that matters most — a skill step still hits OPA and the
human gate, execution grants no extra authority — **does hold** either way,
since both paths route every step through `gateway.call_tool()`. This is a
correctness/reuse gap, not a security hole.

**Also confirmed missing, separately:** there is no data-model support for
the skill↔runbook link at all. `Formula` (`harness_memory/models.py`) has no
`runbook_ref` field, and `SRE_OUTPUT_SCHEMA` (`harness_agents/types.py`) has
no field to cite which skill executed — only `runbook_ref`. User story 38 /
the PRD's "Skill ↔ runbook link" can't be satisfied until this field exists
somewhere.

Concrete remaining work (revises the ACs below):

1. Have the SRE agent call the existing `GatewayClient.execute_skill()` /
   `SkillRunner` machinery instead of `_execute_formula_steps()`'s duplicate —
   fixes the `on_failure` gap for free and gets revocation/signal checks that
   don't exist today.
2. Expose it as an LLM-callable tool for the `sre` role (mirror
   `review_server`'s `run_skill`, wired into `TOOL_NAME_MAP`/`allowed_tools`/
   the prompt) so the LLM can discover via `skill_search` and explicitly
   choose to call `run_skill(<id>)`, matching the PRD's design and restoring
   the judgment call the current forced preload removes. Whether the harness
   *also* keeps preloading a high-confidence formula as a prompt hint (as it
   does today) is a smaller, reasonable choice to keep — the gap is the
   missing tool, not the preload itself.
3. Add a `runbook_ref` field to `Formula` and a matching field to
   `SRE_OUTPUT_SCHEMA` so a report can cite which skill ran and the runbook it
   documents.
4. Share the `score` fix from issue 05 — `skill_search`/guidance can't rank
   confidently without it.

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 4 (consumption + precedence).

## What to build

Make the dynamic SRE agent consult *both* knowledge tiers when it looks for
"what to do," and prefer the higher-trust, executable one. Runbooks (slice 3) are
the advisory prior; learned skills (`DoltFormulaStore`, surfaced via
`skill_search` from issue 05) are the executable posterior, run via `run_skill`
with per-step OPA re-check.

Behaviour: when investigating an incident signature the agent obtains both a
skill match (issue 05) and a runbook match (issue 04). A confidently-matching
ACTIVE skill **outranks** a runbook — the agent is steered to `run_skill(<id>)`
rather than improvise. When no skill matches (cold-start), it falls back to
reading the runbook and reasoning. Confidence is a tunable threshold, separate
from the runbook relevance threshold.

Executing a skill is **not** a shortcut past authorization: `run_skill`
re-checks every step against OPA with the SRE token (promotion grants no
authority). A skill step that calls `shell_exec` still routes through the
existing human gate. The report cites both the executed skill id and the skill's
linked `runbook_ref`, so `runbook_ref` is satisfied from the skill when one ran.

The agent's resolved investigations are the episodes the skill-learning pipeline
consumes (captured on the existing governance audit path) — this slice builds no
new capture mechanism, it only ensures the agent's runs produce that signal so
the learning loop can later mint skills the agent then discovers.

External dependency: `run_skill` is delivered by the skill-learning PRD. ~~Until
it exists, the agent's call to `run_skill` is recorded/mocked in tests; with no
skills seeded, guidance degrades gracefully to runbook-only.~~ **Update
2026-08-03: this dependency has landed** — `GatewayClient.execute_skill()` /
`SkillRunner` exist and are already used by `review_server`'s `run_skill` MCP
tool (see Implementation status above). The SRE agent just never got wired to
consume it.

## Revised acceptance criteria (2026-08-03)

- [ ] SRE-role skill execution goes through `GatewayClient.execute_skill()` / `SkillRunner`, not a separate hand-rolled step loop — so `on_failure` (ABORT/CONTINUE/ROLLBACK), `expected_signal`, and governance-served revocation checks apply
- [ ] A `run_skill` tool is registered for the `sre` role (`TOOL_NAME_MAP` + `allowed_tools` + prompt) so the LLM can call it explicitly by id after `skill_search`
- [ ] Skill precedence: an ACTIVE, confidently-matching skill ranks above the runbook and the agent drives a `run_skill` call carrying the skill id (not an improvised tool sequence)
- [ ] Cold-start fallback: with no matching skill, the agent uses the runbook and never calls `run_skill`
- [ ] Stale-skill fallback: when the only match is expired/revoked it is excluded and guidance falls back to runbooks
- [ ] `Formula` gains a `runbook_ref` field and `SRE_OUTPUT_SCHEMA` gains a field for the executed skill id, so a report can cite both
- [ ] Report linkage: a run via a skill with a linked `runbook_ref` produces a report whose `runbook_ref` is populated from the skill
- [ ] Unit tests use a fake skill store + recording gateway; one integration test seeds an ACTIVE skill and drives discovery + execution end-to-end through the live gateway with OPA in the path
- [ ] Docs updated when green

<details>
<summary>Original acceptance criteria (2026-08-02, superseded — kept for history)</summary>

- [ ] Skill precedence: an ACTIVE, confidently-matching skill ranks above the runbook and the agent drives a `run_skill` call carrying the skill id (not an improvised tool sequence)
- [ ] Cold-start fallback: with no matching skill, the agent uses the runbook and never calls `run_skill`
- [ ] Stale-skill fallback: when the only match is expired/revoked it is excluded and guidance falls back to runbooks
- [ ] Safety backstop: an executed skill whose step the gateway denies surfaces `tool_access_denied`, proving execution is not a shortcut past authorization
- [ ] Report linkage: a run via a skill with a linked `runbook_ref` produces a report whose `runbook_ref` is populated from the skill
- [ ] Unit tests use a fake skill store + recording gateway; one integration test seeds an ACTIVE skill and drives discovery + execution end-to-end through the live gateway with OPA in the path
- [ ] Docs updated when green

</details>

## Blocked by

- 01
- 04
- 05

Issue 01 — DynamicSREAgent ReAct loop (done). Issue 04 — Semantic
runbook_read over the seeded corpus. Issue 05 — skill_search discovery tool.
