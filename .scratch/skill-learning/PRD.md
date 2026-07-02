# Skill Learning

Procedural, governed self-learning: the harness promotes recurring successful remediations into versioned, expiring, HITL-gated skills. No model weights change — every learned behaviour is diffable, attributable, and revocable.

Source spec: SKILL-LEARNING-SPEC.md (Google Doc)

# SKILL-LEARNING-SPEC.md

**Status:** Draft **Depends on:** `ARCHITECTURE.md`, `AGENT-ORCHESTRATION-SPEC.md` **Audit store:** Dolt (tamper-evident, versioned) **Authorization:** OPA policy-as-code

---

## 1\. Purpose & Scope

This spec defines how `ai-harness` learns from resolved remediations by promoting recurring **episodes** into reusable, governed **skills**. It covers the data model, the episode → candidate → skill lifecycle, promotion criteria, human-in-the-loop (HITL) gating, skill expiry, conflict resolution, and the poisoning threat model.

Learning here is **procedural, not parametric**: no model weights change. A skill is a structured, reviewed remediation procedure derived from evidence in the Dolt audit log. This keeps every learned behaviour diffable, attributable, and revocable — consistent with the harness's treatment of agents as untrusted principals.

**Out of scope (this revision):** automated promotion (HITL is mandatory for the medium term), fine-tuning / preference learning, cross-tenant skill sharing.

### 1.1 Constraint tags

- `[HARD]` — invariant; violation is a defect.  
- `[SOFT]` — strong default; deviation requires a Decision Log entry.

---

## 2\. Definitions

| Term | Definition |
| :---- | :---- |
| **Episode** | One completed remediation attempt: an immutable record of symptom, diagnosis, ordered actions, outcome, and environment fingerprint. |
| **Candidate** | A cluster of similar episodes converging on a common successful remediation, proposed for promotion. Not yet executable. |
| **Skill** | A reviewed, versioned, executable remediation procedure. A composite capability subject to the same scoped-token authorization as its constituent tool calls. |
| **Promotion** | The HITL-gated event that turns a candidate into a skill (or a new skill version). |
| **Environment fingerprint** | A structured snapshot of the conditions under which an episode occurred (service version, region, dependency versions, alert source), used for drift detection and conflict resolution. |

---

## 3\. Data Model (Dolt)

All tables are append-oriented; corrections are new rows, never mutations. Dolt commit hashes provide provenance for free.

### 3.1 `episodes` `[HARD]`

episode\_id          UUID            PK

created\_at          TIMESTAMP

agent\_principal     TEXT            \-- which untrusted principal ran this

alert\_signature     TEXT            \-- normalized alert/symptom key

service\_class       TEXT            \-- e.g. "stateless-api", "stateful-queue"

env\_fingerprint     JSON            \-- see 2\. Definitions

diagnosis           JSON            \-- structured root-cause hypothesis

actions             JSON            \-- ordered list of {tool, scoped\_args, scope\_token\_ref}

outcome             ENUM            \-- RESOLVED | FAILED | ROLLED\_BACK | HUMAN\_OVERRIDE | INCONCLUSIVE

outcome\_signal      JSON            \-- post-action metrics that justify the outcome label

outcome\_labeled\_at  TIMESTAMP       \-- null until outcome is confirmed

human\_actor         TEXT            \-- null unless a human intervened

- `[HARD]` `actions[].scope_token_ref` MUST reference the actual scoped token used at runtime, so a learned skill inherits provably-real authorization scopes rather than invented ones.  
- `[HARD]` `outcome` MUST NOT be set to `RESOLVED` by the same agent principal that performed the actions without an independent `outcome_signal`. Self- declared success is the primary survivorship/poisoning vector (see §8).

### 3.2 `candidates`

candidate\_id        UUID            PK

created\_at          TIMESTAMP

cluster\_key         TEXT            \-- (alert\_signature, service\_class) by default

member\_episode\_ids  JSON            \-- episodes supporting this candidate

proposed\_procedure  JSON            \-- normalized skill body (see §4)

support\_stats       JSON            \-- see §5

status              ENUM            \-- PROPOSED | UNDER\_REVIEW | PROMOTED | REJECTED

### 3.3 `skills` `[HARD]`

skill\_id            UUID            PK

version             INT

created\_at          TIMESTAMP

promoted\_by         TEXT            \-- human actor (HITL); \[HARD\] non-null

source\_candidate\_id UUID            \-- provenance back to episodes

procedure           JSON            \-- the executable skill body (§4)

status              ENUM            \-- ACTIVE | EXPIRED | REVOKED | DEPRECATED

expires\_at          TIMESTAMP       \-- see §6

revoked\_reason      TEXT            \-- null unless REVOKED

- `[HARD]` `promoted_by` MUST be a human actor for the medium term. An empty or agent-valued `promoted_by` is a defect.  
- `[HARD]` Every `skill` row MUST be traceable to `member_episode_ids` via `source_candidate_id`. Orphan skills are forbidden.

---

## 4\. Skill Body Format

A skill is a typed, parameterized procedure — the generalized form of the episodes that justified it. It contains no weights and no free-form model output at execution time beyond parameter binding.

{

  "preconditions": {

    "alert\_signature": "\<pattern\>",

    "service\_class": "\<class\>",

    "env\_constraints": \[ "dependency\>=x.y", "region in \[...\]" \]

  },

  "steps": \[

    {

      "tool": "\<mcp\_tool\_id\>",

      "args\_template": { "...": "\<bound at runtime\>" },

      "required\_scope": "\<scope descriptor\>",

      "expected\_signal": { "...": "intermediate success check" },

      "on\_failure": "ABORT | CONTINUE | ROLLBACK"

    }

  \],

  "success\_criteria": { "...": "post-execution signal" },

  "rollback": \[ { "tool": "...", "args\_template": {} } \]

}

- `[HARD]` Every `steps[].required_scope` MUST be authorizable under current OPA policy at execution time. A skill never carries ambient authority; it is re-checked per step against the invoking principal's scoped tokens, exactly as if the steps were issued directly. Promotion does NOT grant privilege.  
- `[SOFT]` Every skill SHOULD define a `rollback`. Skills mutating stateful services without rollback require a Decision Log entry.  
- `[HARD]` `success_criteria` MUST be machine-checkable and MUST NOT depend solely on the agent's own assertion of success.

---

## 5\. Promotion Criteria

Clustering produces candidates; criteria below determine **eligibility for human review**. They are necessary, not sufficient — the human is the sufficient condition (§7). Defaults are `[SOFT]` and policy-tunable.

A candidate becomes eligible (`status = PROPOSED → UNDER_REVIEW`) when all hold:

1. **Volume.** `>= N_min` member episodes with `outcome = RESOLVED` (default `N_min = 5`).  
2. **Independence.** Successes span `>= K` distinct `human_actor` / `agent_principal` pairs (default `K = 2`) — guards against correlated success (one operator repeating a lucky-but-wrong fix).  
3. **Recency.** `>= M` supporting episodes within the trailing window (default `M = 2`, window `90d`) — guards against stale remediations.  
4. **Outcome integrity.** No supporting episode relies on self-declared success (§3.1); each has an independent `outcome_signal`.  
5. **Environment coherence.** Member episodes' `env_fingerprint`s are mutually compatible; divergent environments either narrow the candidate's `preconditions` or split it into multiple candidates.

`support_stats` records each criterion's computed value so the reviewer sees *why* a candidate qualified, not just that it did.

### 5.1 Survivorship note `[HARD]`

The episode population is biased toward remediations that reached a terminal outcome. Promotion criteria MUST be computed only over episodes with a confirmed `outcome_labeled_at`; episodes still `INCONCLUSIVE` are excluded from both numerator and denominator to avoid inflating apparent success rates.

---

## 6\. Skill Expiry & Re-validation

Skills are **expiring assets**, not permanent runbooks.

- `[HARD]` Every `ACTIVE` skill has a non-null `expires_at` (default lifetime `[SOFT]` 90d).  
- On expiry, status transitions `ACTIVE → EXPIRED`. An `EXPIRED` skill is not executable.  
- Re-validation: a fresh clustering pass over recent episodes can re-propose an expired skill as a candidate. Re-promotion (HITL) yields a new `version` with a refreshed `expires_at`, preserving the lineage.  
- `[SOFT]` A skill whose recent episodes show declining success SHOULD be flagged for early review before `expires_at`.

This converts drift from a silent failure into a scheduled review event.

---

## 7\. HITL Promotion Gate

- `[HARD]` Promotion (`candidate → skill`, any version) requires an explicit human action recorded as `promoted_by`. Automated promotion is forbidden in this revision.  
- The reviewer is presented: the proposed procedure, `support_stats`, the full member-episode set (including any failures sharing the cluster key), and a diff against the prior version if re-promoting.  
- `[HARD]` Promotion is itself an OPA-authorized action — only principals holding a `skill:promote` scope may commit. The promotion event is written to Dolt with the reviewer identity and commit hash.  
- `[SOFT]` Rejections SHOULD record a reason; rejected candidates remain queryable so the same bad pattern isn't re-surfaced indefinitely.

---

## 8\. Threat Model

A learned-skill pipeline is a **privilege-persistence path**: a malicious remediation, once promoted, executes under future authorization checks because it is now "a skill." Mitigations:

| Threat | Mitigation |
| :---- | :---- |
| **Episode poisoning** — attacker manufactures episodes to manufacture a candidate. | Independence criterion (§5.2), HITL review of member episodes, immutable provenance. Red-team target: "can crafted episodes promote a malicious skill?" |
| **Self-declared success** — agent labels its own bad actions RESOLVED. | `[HARD]` independent `outcome_signal` required (§3.1, §5.4). |
| **Privilege escalation via skill** — skill carries broader scope than its principal. | `[HARD]` per-step OPA re-check at execution; promotion grants no authority (§4). |
| **Drift exploitation** — once-valid skill becomes harmful as environment changes. | Expiry \+ re-validation (§6); environment coherence (§5.5). |
| **Prompt-injection → episode** — injected content steers an agent into generating a poisonable episode. | Extends existing prompt-injection red-teaming; episodes from flagged sessions excluded from clustering. |
| **Silent skill mutation** — skill body altered post-promotion. | Append-only Dolt model; any change is a new version requiring HITL. |

`[HARD]` Any promoted skill MUST be revocable in a single action (`status → REVOKED`), immediately removing it from execution selection, with the reason recorded.

---

## 9\. Conflict Resolution

When multiple `ACTIVE` skills match an incident, selection is an OPA-shaped decision, not an agent choice:

1. **Precondition specificity** — most specific matching skill wins.  
2. **Recency of validation** — more recently (re-)promoted wins ties.  
3. **Trailing success rate** — higher recent success rate breaks remaining ties.  
4. `[HARD]` If no deterministic winner remains, the harness MUST escalate to a human rather than pick arbitrarily.

Selection rationale is logged per invocation for auditability.

---

## 10\. Lifecycle Summary

   resolved remediation

          │

          ▼

     \[ episode \]  ──(immutable, labeled)──► Dolt

          │

          │ clustering pass (§5)

          ▼

    \[ candidate \]  ──(criteria met)──► UNDER\_REVIEW

          │

          │ HITL gate (§7)  ── reject ──► REJECTED (retained)

          ▼

      \[ skill v1 \]  ── ACTIVE, expires\_at set

          │

          ├── per-step OPA re-check on every execution (§4)

          ├── expiry (§6) ──► EXPIRED ──► re-validation ──► skill v2

          └── revoke (§8) ──► REVOKED

---

## 11\. MVP vs. Full

| Capability | MVP | Full |
| :---- | :---- | :---- |
| Episode capture & labeling | ✅ | ✅ |
| Manual candidate proposal | ✅ | — |
| Automated clustering → candidate | — | ✅ |
| HITL promotion | ✅ | ✅ |
| Per-step OPA re-check | ✅ | ✅ |
| Expiry / re-validation | `[SOFT]` manual | ✅ scheduled |
| Conflict resolution policy | single-skill only | ✅ multi-skill |
| Automated promotion | ❌ (forbidden) | future revision |

**MVP principle:** don't build promotion machinery before real episodes exist to populate it. Start with manual candidate proposal \+ HITL commit; observe what good candidates look like; automate clustering only once the shape is known.

---

## 12\. Open Questions

- Episode clustering key: is `(alert_signature, service_class)` sufficient, or is a learned similarity metric warranted once volume grows?  
- Should `outcome_signal` schemas be per-service-class (richer, more maintenance) or universal (simpler, coarser)?  
- Cross-environment skill portability: when is a skill validated in staging trustworthy in production?

---

## 13\. Decision Log

| Date | Decision | Rationale |
| :---- | :---- | :---- |
| *draft* | HITL promotion mandatory, medium term | Promotion is a privilege-persistence path; automated promotion deferred until threat model is validated by red-teaming. |
| *draft* | Skills expire by default | Drift is a silent failure; expiry converts it to a scheduled review. |
| *draft* | Procedural, not parametric, learning | Keeps learned behaviour diffable, attributable, revocable — consistent with untrusted-principal model. |


## Issues

| # | Title | Type | Blocked by |
|---|-------|------|------------|
| [01](issues/01-dolt-schema-episodes-candidates-skills.md) | Dolt schema: episodes, candidates, skills + migrate formulas | AFK | — |
| [02](issues/02-episode-capture.md) | Episode capture on governance audit path | AFK | 01 |
| [03](issues/03-outcome-labeling-endpoint.md) | Independent outcome labeling endpoint | AFK | 02 |
| [04](issues/04-manual-candidate-proposal.md) | Manual candidate proposal | AFK | 03 |
| [05](issues/05-hitl-promotion-gate.md) | HITL promotion gate | HITL | 04 |
| [06](issues/06-skill-execution-revocation.md) | Skill execution with per-step OPA re-check and revocation | AFK | 05 |
| [07](issues/07-skill-expiry-revalidation.md) | Skill expiry and lightweight re-validation trigger | AFK | 06 |
| [08](issues/08-conflict-resolution-escalation.md) | Skill conflict resolution and human escalation | AFK | 06 |
