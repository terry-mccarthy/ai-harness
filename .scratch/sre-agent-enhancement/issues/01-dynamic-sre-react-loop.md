---
title: "DynamicSREAgent ReAct loop"
status: ready-for-agent
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 1.

## What to build

Replace the linear "gather once, retry only on schema failure" `SREAgent` with a
`DynamicSREAgent` that runs a ReAct tool-use loop, modelled on the existing
`DynamicCodeReviewerAgent`. On each turn the LLM emits exactly one JSON action —
`call_tool` or `respond` — and the agent dispatches it, feeds the tool result
back as the next turn, and loops up to a turn cap before producing a
schema-validated incident report.

End-to-end behaviour: an `incident`-classified task is routed by the supervisor
to the dynamic SRE agent, which decides which of `observability_query`,
`log_search`, `runbook_read` to call next based on what it just observed
(non-linear), and returns a report satisfying `SRE_OUTPUT_SCHEMA`. The agent
never calls `shell_exec` itself — it proposes it in `recommended_steps` with
`requires_approval=true` and sets `requires_human_approval=true`; the existing
human gate and OPA enforce it.

The existing stub tools are fine for this slice (they still return stubs);
realism for logs/runbooks lands in later slices. The static `SREAgent` is
retired — no silent dual path.

### Per-turn action contract (from the reviewer ReAct loop)

```
# call a tool
{"action": "call_tool", "tool": "log_search", "params": {"query": "5xx checkout"}}

# deliver the final report (validated against SRE_OUTPUT_SCHEMA)
{"action": "respond", "result": {
  "timeline": "...", "likely_cause": "...", "severity": "P2",
  "recommended_steps": [{"action": "...", "rationale": "...", "requires_approval": false}],
  "runbook_ref": "RB-014", "requires_human_approval": false
}}
```

Add a `react_sre.md` system prompt analogous to `react_code_reviewer.md`. Reuse
the reviewer loop's small single-responsibility helpers (`_handle_respond_action`,
`_handle_tool_call`, `_dispatch_action`) to keep CCN low.

## Acceptance criteria

- [ ] Multi-turn happy path: scripted turns drive a tool sequence then `respond`; the recorded `call_tool` order matches and `agent_output` is schema-valid
- [ ] Non-linear path: a turn re-queries a tool with refined params after seeing a result; the second call carries the refined params
- [ ] Bounded loop: a never-respond LLM yields `error.code == "max_turns_exceeded"`
- [ ] A malformed (non-JSON) turn produces a corrective re-prompt, not a crash
- [ ] An invalid final `respond` (schema violation) produces a corrective re-prompt
- [ ] Injection safety: an LLM that "obeys" injected evidence by requesting `shell_exec` against a denying gateway yields `error.code == "tool_access_denied"`
- [ ] `requires_human_approval` is true whenever any recommended step involves `shell_exec`
- [ ] Token usage is accumulated across every turn; a low `token_budget` aborts with `token_budget_exceeded`
- [ ] Past-incident context is loaded into the opening message; a resolved report is written back to the `sre` memory namespace
- [ ] Supervisor routes `incident` tasks to the dynamic SRE agent; static `SREAgent` is removed with no remaining references
- [ ] Unit tests run without the Docker stack; one integration test drives the agent through the live gateway on an `incident` task
- [ ] Docs updated (`CLAUDE.md`, `ARCHITECTURE.md`, `README.md`, `PROGRESS.md`) when green

## Blocked by

None - can start immediately.
