Status: done

# 04 — agent_invoke: synchronous governed handoff

## What to build

Add a `POST /agent/invoke` endpoint to the governance service that lets a caller synchronously hand off work to a named agent.

The critical security property: the target agent always runs under **its own** client credentials and role scopes. The caller's token is never forwarded. This means governance must mint (or fetch from cache) the target's own OAuth token before forwarding through MCPJungle. A prompt-injected or misconfigured caller cannot use `agent_invoke` to escalate the target's privileges.

The endpoint must: validate the caller JWT, query OPA for `may <caller_role> invoke <target>?`, validate the payload against the target's declared `input_schema` (returning 422 for malformed payloads), forward the call through MCPJungle using the target's own token, write an audit row for both allowed AND denied invocations (a denied attempt must still be recorded), and return the target's structured output.

## Acceptance criteria

- [ ] `test_agent_invoke_allowed`: supervisor successfully invokes `code-reviewer`; structured result returned
- [ ] `test_agent_invoke_denied_is_403_and_audited`: reviewer attempting to invoke `sre` receives 403 AND an audit row exists for the denied attempt
- [ ] `test_invoke_uses_target_credentials`: the token forwarded to MCPJungle belongs to the target role, not the caller
- [ ] `test_invoke_rejects_malformed_payload`: payload failing `input_schema` returns 422 before any OPA or network call
- [ ] `test_injection_via_task_payload_denied`: a task payload containing an injected instruction to invoke `sre/shell_exec` is blocked by the topology policy and audited
- [ ] 404 returned when target is unknown or unhealthy

## Blocked by

- Issue 02 (OPA invoke topology must be in place)
