# Skills — Procedural Skill Learning

Skills are versioned, HITL-gated remediation procedures. They are learned from observed agent behaviour (the episode pipeline) or authored manually by a human operator. Once promoted, skills guide future SRE investigations and are exposed as Claude Code slash commands.

## Lifecycle

```
Agent run → episode captured → label outcome → propose candidate → human promotes → active skill
                                                                                        ↓
                                                                              expires after 90 days
                                                                              (auto-propose renewal)
```

### Episode capture

Every SRE agent run produces an episode row in Dolt via `POST /audit`. Episodes record the task, tool calls made, and the raw outcome.

### Outcome labeling

A human (or the SRE agent itself, for clear-cut cases) labels the episode as `success` or `failure` via `POST /episodes/{id}/label`. Episodes without a label cannot advance.

### Candidate proposal

Successful episodes can be proposed as candidates via `POST /candidates`. The candidate contains extracted steps, a description, and `task_patterns` that determine when the skill should be selected.

### HITL promotion

A human operator reviews the candidate and either promotes it (`POST /candidates/{id}/promote`) or rejects it (`POST /candidates/{id}/reject`). Promotion writes the skill to the `skills` table with `status=active` and a Dolt commit.

### Expiry and renewal

Skills expire after 90 days. `POST /skills/expire` marks expired skills as `expired` and auto-proposes renewal candidates for any that met the success threshold in their last 90-day window.

## Manual authoring

Skills can be created directly without going through the episode pipeline:

```bash
# From Claude Code
/skill-create
```

Or via the MCP tool:

```
registry__create_skill  →  skills-registry-server
```

Authored skills are immediately active (`status=active`, `expires_at=now+90d`) and appear in `POST /skills/select` results. The `manually_authored` column distinguishes them from pipeline-promoted skills.

## Skill selection

`POST /skills/select` deterministically picks the best skill for a given task using three tiebreak rules:

1. **Specificity** — more specific `env_constraints` win over generic ones
2. **Recency** — newer skills win when specificity is equal
3. **Success rate** — higher `quality_score` wins when recency is equal

If no skill matches, the SRE agent reasons freely and the novel behaviour is captured as a new episode.

## Management slash commands

The following commands are available in Claude Code once the harness stack is running:

| Command | What it does |
|---|---|
| `/skills-list` | List active, expired, and revoked skills |
| `/skill-view` | Inspect a skill's steps, patterns, and metadata |
| `/skill-create` | Author a new skill directly (human-operator only) |
| `/skill-label` | Label an episode outcome as success or failure |
| `/skill-propose` | Propose a candidate from a labeled episode |
| `/skill-promote` | Promote a candidate to active skill (human-operator only) |
| `/skill-reject` | Reject a candidate with a reason |
| `/skill-revoke` | Revoke an active skill |
| `/episodes-list` | List recent episodes with their labeling status |
| `/sync-skills` | Regenerate `.claude/commands/skill-*.md` from the registry |

All commands call the `skills-registry-server` MCP tools (`registry__*`), which forward to governance with `human-operator` credentials.

## Generated skill commands

Active skills are synced to `.claude/commands/skill-<name>.md` by:

```bash
make sync-skills
```

Each generated file is a Claude Code slash command that loads the skill's prompt template and optionally invokes `registry__execute_skill`. Generated files are gitignored (`skill-*.md`) — run `make sync-skills` after starting the stack or after promoting new skills.

## Skills registry server (`:9006`)

`services/skills_registry/server.py` — FastMCP service that wraps all governance skill endpoints as MCP tools. It uses `human-operator` OAuth credentials and is the single surface through which Claude Code accesses the registry.

Every tool call through the registry server:
- Calls `POST /check` on governance (OPA-enforced)
- Produces an audit row in Dolt with a DOLT_COMMIT
- Per-step OPA re-check when `registry__execute_skill` runs

## Dolt audit trail

```sql
-- All manually authored skills
SELECT * FROM skills WHERE manually_authored = 1;

-- Pipeline-promoted skills
SELECT * FROM skills WHERE manually_authored = 0;

-- Full version history for a skill
SELECT * FROM dolt_log WHERE message LIKE '%<skill_id>%';

-- Gate failures from architect reviews
SELECT * FROM architectural_gate_failures ORDER BY created_at DESC;
```
