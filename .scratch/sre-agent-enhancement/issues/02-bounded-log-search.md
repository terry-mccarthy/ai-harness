---
title: "Bounded log_search over a seeded log source"
status: ready-for-agent
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 2.

## What to build

Make the `sre_stub` `log_search` tool return real, bounded, ranked matches
against a seeded sample log source baked into the container image — mirroring how
`linter_stub` runs semgrep against seeded input (rules/sample shipped in the
image, not fetched at runtime).

Behaviour: given a `query` string, return a structured result containing the
matched lines ranked most-relevant first, the count returned, and the total count
of matches found (so the agent can detect truncation). Output is capped at a
maximum number of lines (a module constant) regardless of how many lines match,
protecting the agent's context window. Ranking is a simple, explainable,
dependency-free scheme (substring / term overlap) — not an embedding model. A
no-match query returns a well-formed empty result, never an error.

This is the supporting plumbing that stops a realistic `log_search` from flooding
the context window the ReAct loop depends on. Keep it minimal.

## Acceptance criteria

- [ ] A query returns only lines relevant to it, drawn from the seeded log source
- [ ] Results are ranked most-relevant first
- [ ] Output is capped at the max-line constant even when more lines match
- [ ] The result reports returned-count and total-match-count so truncation is detectable
- [ ] A no-match query returns a well-formed empty result (empty matches, zero counts), not an error
- [ ] Tests exercise the tool against the seeded source without the full Docker stack
- [ ] Docs updated when green

## Blocked by

None - can start immediately.
