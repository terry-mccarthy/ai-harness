---
title: "Dolt schema: episodes, candidates, skills tables + migrate formulas"
status: ready-for-agent
type: AFK
---

## What to build

Extend `services/dolt/init.sh` with three new tables (`episodes`, `candidates`, `skills`) and migrate the existing hand-seeded `formulas` rows into `skills` as the v1 baseline — preserving the seeded data as `source_candidate_id = NULL` (no episode provenance, noted in a comment).

The new tables implement the data model from the skill-learning spec:

- **`episodes`** — one row per completed tool-call remediation attempt. `outcome` and `outcome_labeled_at` start NULL; a separate endpoint (issue 03) labels them. `agent_principal`, `alert_signature`, `service_class`, `env_fingerprint` (JSON), `diagnosis` (JSON), `actions` (JSON array of `{tool, scoped_args, scope_token_ref}`), `outcome_signal` (JSON), `human_actor` (nullable).
- **`candidates`** — clusters of similar episodes proposed for promotion. `cluster_key` TEXT, `member_episode_ids` (JSON), `proposed_procedure` (JSON, skill body shape), `support_stats` (JSON), `status` ENUM `PROPOSED|UNDER_REVIEW|PROMOTED|REJECTED`.
- **`skills`** — promoted, versioned, executable procedures. `version` INT, `promoted_by` TEXT NOT NULL, `source_candidate_id` UUID (FK to candidates), `procedure` (JSON), `status` ENUM `ACTIVE|EXPIRED|REVOKED|DEPRECATED`, `expires_at` TIMESTAMP, `revoked_reason` TEXT nullable.

Grant the `harness` DB user `SELECT, INSERT` on `episodes`; `SELECT, INSERT, UPDATE` on `candidates` and `skills`. Grant `SELECT` on `dolt_log` for provenance queries.

Drop `formulas` and `formula_pours` after migrating their data into `skills` (as seed rows with `source_candidate_id = NULL`). Update `DoltFormulaStore` to read/write from `skills` with the new column names.

## Acceptance criteria

- [ ] `docker compose build dolt && docker compose up -d --no-deps dolt` succeeds with no errors
- [ ] All three new tables exist and are queryable via the `harness` user
- [ ] Seeded skill rows (sre:triage-incident, code_reviewer:review-pr, architect:write-adr) present in `skills` with `source_candidate_id = NULL`
- [ ] `formulas` and `formula_pours` tables no longer exist
- [ ] `DoltFormulaStore` tests pass against the renamed schema
- [ ] `make test-integration` still green

## Blocked by

None — can start immediately.
