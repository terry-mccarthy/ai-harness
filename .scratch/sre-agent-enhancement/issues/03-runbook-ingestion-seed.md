---
title: "Runbook ingestion seed into pgvector"
status: ready-for-agent
type: AFK
---

## Parent

[SRE Agent Enhancement PRD](../PRD.md) — Slice 3 (ingestion mechanism).

## What to build

A seed step that ingests the existing `docs/runbooks/*.md` files — currently
orphaned, read by no code — into `PostgresMemoryStore` (pgvector,
`nomic-embed-text`) so they become the canonical runbook corpus.

For each runbook file: embed the `**When to use:**` line as the searchable
*signature*, store the full markdown body as the entry value, key the entry by
the filename slug (e.g. `cost-spike`), under a dedicated `runbooks` namespace
(separate from the `sre` incident-memory namespace). A file missing its
`**When to use:**` line is skipped with a logged warning, never ingested with an
empty signature.

Expose the seed both as a `make seed-runbooks` target and as an importable
function so a deploy-time job and tests can call it directly. Ingestion is not
done lazily inside `runbook_read` — this slice only writes. Idempotency comes
from keying on slug via the store's `ON CONFLICT (namespace, key)` upsert:
re-seeding an unchanged file is a no-op; an edited file is updated in place;
never duplicated.

Note the hosted caveat (see PRD Further Notes): the runbook files must travel
into any container image, and embeddings require a reachable embedding endpoint.

## Acceptance criteria

- [ ] Running the seed embeds each runbook's `**When to use:**` signature and stores its body under the `runbooks` namespace, keyed by filename slug
- [ ] The seed is callable both as `make seed-runbooks` and as an importable function
- [ ] Re-running the seed over an unchanged corpus adds no rows; an edited file is updated in place (no duplicates)
- [ ] A runbook file missing its `**When to use:**` line is skipped with a warning
- [ ] Ingestion tests run against a small fixture `docs/runbooks/`-shaped directory, not the real corpus
- [ ] Docs updated when green

## Blocked by

None - can start immediately.
