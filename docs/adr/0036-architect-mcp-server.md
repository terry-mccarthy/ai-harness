# ADR-0036: Architect MCP server — host-side, semble-style, designed for ECS portability

**Status:** proposed
**Date:** 2026-06-17

## Context

`stub_servers/architect_server.py` implements the architect role's four MCP tools
(`codebase_search`, `adr_read`, `adr_write`, `diagram_gen`) as 4-line functions
returning `{"result": "stub", ...}`. `ArchitectAgent`
(`packages/harness-agents/harness_agents/architect.py`) is fully wired — system
prompt, 3-retry JSON-schema validation loop, memory namespace, OAuth client, OPA
policy — but every tool call returns placeholder data, so the agent's output is
unusable.

The four tools need real implementations. The hardest sub-decision is *how the
tools access the codebase being analysed*. Existing containerised stubs
(`git-diff-stub`) bake a sample repo into the image; that approach does not
generalise to "analyse my actual repo".

`semble` (the project's existing code-search MCP, installed as a host-side `uv`
tool) solves exactly this shape: stateless `repo` parameter per call, accepts
local paths or `https://` git URLs, LRU-cached lazy indexing, `watchfiles`
invalidation, hybrid BM25 + dense search.

## Decision

Replicate semble's architecture for the architect MCP server.

- **Lifecycle:** host-side FastMCP streamable-HTTP server. Reached from the
  `review-server` container via `host.docker.internal:90XX`, same pattern as
  Ollama. Started independently of `docker compose up` (e.g. a
  `make architect-up` target).
- **`repo` parameter:** typed reference, accepted by every tool that touches
  code. v1 schemes: `/local/path` and `https://github.com/org/repo[@ref]`.
  v2 (hosted): add `s3://bucket/key.tar.gz` and `upload://<id>`.
- **Indexing:** LRU cache (size ~10) keyed by resolved commit SHA for git URLs
  and resolved path for local repos. `watchfiles.awatch` invalidates the cache
  entry for local paths.
- **Search:** hybrid — BM25 + dense via existing Ollama `nomic-embed-text`
  (consistent with ADR 0022). Modes `hybrid | semantic | bm25` selectable per
  call.
- **ADR storage:** `<repo>/docs/adr/{slug}.md`. `adr_read` and `adr_write`
  operate on the repo passed in `repo`, not a global store.
- **Diagram output:** Mermaid text only.
- **New tool:** `architecture_review(target_mode: "codebase" | "diff", repo, diff?)`
  — single tool with two modes. Scores changes against `ARCHITECTURE.md`
  invariants and `docs/adr/`. Adds one entry to `policies/harness.rego` under
  the `architect` role.

## Architectural review

### Vulnerabilities and bottlenecks

- Host-side process is outside `docker compose` orchestration — no automatic
  restart, no shared log aggregation. Mitigated by `make architect-up` + an
  optional launchd plist for long-running dev use.
- Local-path mode reads anything the host user can read. The OPA policy check
  still runs (governance is mandatory intercept — [HARD]), but the *tool
  implementation* has no filesystem sandbox. Same risk profile as semble itself.
- LRU cache is per-process; restarts cost a re-index. Acceptable for v1;
  revisit if cold starts dominate.

### Required changes

- Add `architecture_review` to the `architect` role in `policies/harness.rego`
  before the tool ships. Tools without policy entries default to deny — this
  invariant is non-negotiable.
- Register the host-side server with MCPJungle via an init container or CLI
  invocation; registration is ephemeral on MCPJungle restart per existing
  convention.

### Suggestions

- Key the cache by content hash (commit SHA) from day one even though v1 does
  not share caches across processes — the key shape makes future migration to
  EFS or S3-backed shared cache trivial.
- Externalise the `architecture_review` prompt as
  `prompts/architecture_review.md` (consistent with ADR 0025).

## Consequences

- **Breaks the "everything under `docker compose up`" convention.** Trade-off
  for native filesystem access, working `watchfiles` (unreliable across Docker
  Desktop bind mounts on macOS), and a real on-ramp to ECS where the same code
  runs by swapping resolvers.
- **Same tool signature in host mode and AWS mode.** The `repo: str` parameter
  is the abstraction boundary; only the resolvers behind it change.
- **Governance invariants unchanged.** Audit log, OPA check, OAuth all apply on
  every call — same trust boundary in AWS does not bypass the audit path.
- **`stub_servers/architect_server.py` retired.** Removed once the new server's
  integration tests pass; the architect role's allowed-tool list in
  `ArchitectAgent` is unchanged apart from the new `architecture_review` entry.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Bind-mount host repo read-only into the existing `architect-stub` container | Works for one repo at a time; no `repo`-per-call flexibility; `watchfiles` unreliable on macOS Docker bind mounts |
| Bake a sample repo into the image (`git-diff-stub` pattern) | Does not generalise to "analyse my actual repo" |
| Delegate `codebase_search` to the existing semble MCP server | Loses control over result shape and the architect-specific tools (`adr_*`, `architecture_review`); couples architect to an external MCP server |
| Stdio MCP server (exactly like semble) | Does not fit MCPJungle's HTTP registration model without a stdio-to-HTTP shim |
| Containerised with on-demand clone only (no host process) | Loses local-path mode; cannot review unpushed working trees during development |
| Bundle a sentence-transformers embedder (semble's choice) | Self-contained but duplicates the embedder ADR 0022 already standardised on; harder to keep aligned with the memory store |
| BM25-only for v1 (defer semantic search) | Faster to ship, but the architect's queries are conceptual ("how is auth wired?"), where BM25 alone is weak |
| Two separate review tools (`review_codebase`, `review_diff_architecture`) | Sharper names but duplicates the prompt and invariant-loading code; promoted to v2 only if usage diverges |
| Render diagrams to PNG via Kroki or `mmdc` | Adds a renderer container; Mermaid text already renders natively on GitHub and inside markdown viewers |
