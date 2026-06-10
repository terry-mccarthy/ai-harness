# OWASP Agentic AI Top 10 — Security Review

**Project:** AI Harness  
**Review date:** 2026-06-10  
**Reviewer:** Phase 5 security review  
**Status:** Approved — residual risks accepted with monitoring

---

## Summary

This document reviews the AI Harness against all ten risks in the OWASP Agentic AI Top 10.
For each risk, the current mitigations are described and any residual risk is stated explicitly.
This document must be reviewed and re-signed on every major release.

---

## Risk 1 — Prompt Injection

**Description:** An attacker embeds instructions inside data the agent processes
(tool responses, user input, retrieved documents) to hijack the agent's behaviour.

**Harness mitigations:**
- All tool responses are treated as untrusted data; they are passed to the LLM as
  `role=user` context, never as `role=system` instructions.
- Each agent's `allowed_tools` list is hard-coded at construction time. No LLM output
  can add a new tool to the set (verified by `test_owasp_prompt_injection_blocked`).
- Gateway enforces tool-scoping per agent role; even if an injected instruction named
  an out-of-scope tool, the OPA policy would deny the call.

**Residual risk:** Indirect prompt injection through long tool responses (e.g. a
codebase search returning malicious content that shifts the LLM's reasoning). Accepted
with monitoring — OTel spans record every LLM call and its token budget.

**Status:** Mitigated

---

## Risk 2 — Insecure Tool Use / Excessive Permissions

**Description:** An agent is granted more tool access than needed, amplifying the
blast radius of a compromise.

**Harness mitigations:**
- OPA policy maps each agent role to a minimal set of tools (architect, code_reviewer,
  sre — no overlap except by explicit policy grant).
- Gateway denies cross-role tool calls at the policy layer before forwarding to the
  MCP backend.
- Rate limiting (20 tool calls/agent/minute) prevents runaway loops even if an agent
  is compromised.

**Residual risk:** None material.

**Status:** Mitigated

---

## Risk 3 — Memory Poisoning

**Description:** An attacker writes malicious content to the agent memory store,
influencing future reasoning.

**Harness mitigations:**
- Memory write endpoint (`POST /memory/write`) requires a valid governance Bearer token
  (verified by `test_owasp_memory_write_requires_auth`).
- Governance validates JWT before forwarding any request; unauthenticated writes
  return 401.
- PostgreSQL row-level security and network policy restrict direct database access.

**Residual risk:** An attacker who has obtained a valid agent token could write to
that agent's namespace. Mitigated by short-lived tokens (15-min TTL) and audit log.

**Status:** Mitigated

---

## Risk 4 — Excessive Agency

**Description:** An agent autonomously takes high-impact actions without human
oversight.

**Harness mitigations:**
- `shell_exec` requires a scoped `human_approval_token` (10-min TTL, bound to
  `thread_id` + tool name). No other path to execute shell commands exists.
- Token scoping prevents reuse across threads or for different tools.
- Rate limiting bounds the total number of tool calls per agent per minute.

**Residual risk:** None material for current tool set.

**Status:** Mitigated

---

## Risk 5 — Insecure Output Handling

**Description:** Agent output is trusted and rendered or executed without validation,
enabling downstream injection.

**Harness mitigations:**
- Every agent output is validated against a JSON Schema contract before being
  returned to callers (Phase 3 DoD item 20).
- `final_response` is plain text — no HTML, no executable content.

**Residual risk:** Structured output from the LLM can contain hallucinated data that
passes schema validation but contains incorrect values (e.g. a fabricated CVE number).
Accepted — callers must treat agent output as advisory, not authoritative.

**Status:** Partial mitigation — residual risk accepted

---

## Risk 6 — Data Exfiltration via Tool Calls

**Description:** An agent is tricked into calling a tool that sends sensitive data to
an attacker-controlled endpoint.

**Harness mitigations:**
- Tool registry in MCPJungle/ContextForge is controlled by administrators; agents
  cannot register new tools at runtime.
- OPA policy whitelists specific tool names per role; no wildcard grants.
- Docker network policy: agent containers have no outbound internet access; all
  traffic is routed through the governance layer.

**Residual risk:** A compromised governance service could forward to an external
endpoint. Mitigated by governance code review on every release.

**Status:** Mitigated

---

## Risk 7 — Supply Chain Compromise

**Description:** A malicious actor injects backdoors via a compromised dependency,
model, or tool server.

**Harness mitigations:**
- All Python dependencies are pinned in `uv.lock` and built via reproducible Docker
  layers.
- OPA policy and MCP stub implementations are version-controlled in this repository.
- LLM models are pulled from Ollama by name+digest; digest should be pinned for
  production.

**Residual risk:** Third-party model weights are not formally audited. LLM providers
(Ollama, Gemini) are trusted as external services. Accepted with quarterly dependency
review.

**Status:** Partial mitigation — residual risk accepted

---

## Risk 8 — Insecure Inter-Agent Communication

**Description:** Messages between agents are not authenticated or integrity-protected,
allowing spoofing or tampering.

**Harness mitigations:**
- All agent-to-agent and agent-to-governance calls carry HS256-signed JWT tokens.
- Governance validates every incoming JWT before policy evaluation.
- Human approval tokens are scoped and short-lived (10-min TTL).

**Residual risk:** HS256 with a shared secret — if `JWT_SECRET` is leaked, tokens can
be forged. Mitigated by secret rotation policy and never storing `JWT_SECRET` in code.

**Status:** Mitigated

---

## Risk 9 — Unbounded Consumption / Denial of Service

**Description:** An attacker or runaway agent exhausts computational or financial
resources.

**Harness mitigations:**
- Token budget per thread (`token_budget` in `HarnessState`); graph terminates with
  `budget_exceeded` when exceeded (verified by `test_token_budget_enforced`).
- Rate limiting: 20 tool calls/agent/minute; returns 429 on excess (verified by
  `test_rate_limit_tool_calls`).
- LLM call timeout: 120-second hard timeout on Ollama requests.
- OTel cost attribution per role: Grafana dashboard alerts on 2× expected spend.

**Residual risk:** Budget tracking relies on LLM-reported token counts, which may
under-count for some models. Accepted.

**Status:** Mitigated

---

## Risk 10 — Agentic System Impersonation

**Description:** An attacker impersonates a legitimate agent to gain elevated access.

**Harness mitigations:**
- Each agent client has a unique `client_id` and `client_secret` issued by governance.
- JWTs embed `role` and `sub` claims; governance enforces role-based tool access.
- Token TTL is 15 minutes; stolen tokens expire quickly.

**Residual risk:** Client secrets are stored in environment variables; a host
compromise exposes them. Mitigated by secret rotation and least-privilege host access.

**Status:** Mitigated

---

## Residual Risks Summary

| Risk | Residual | Accepted? |
|---|---|---|
| LLM hallucination in structured outputs | Yes | Yes — advisory output only |
| Timing side-channels on policy decisions | Yes | Yes — low exploitability |
| Model weight provenance | Yes | Yes — quarterly review |
| HS256 secret leak enables token forgery | Yes | Yes — rotation policy in place |

---

*This document is reviewed on every major release. Last reviewed: 2026-06-10.*
