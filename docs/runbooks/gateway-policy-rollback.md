# Runbook: Gateway Policy Rollback

**When to use:** An OPA policy push has denied all (or too many) tool calls —
agents are getting unexpected 403s and work is blocked.

---

## 1. Confirm the symptom

```bash
# Check recent 403 rate in audit log
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT policy_decision, COUNT(*) as n
  FROM audit_log
  WHERE timestamp_ms > UNIX_TIMESTAMP(NOW() - INTERVAL 5 MINUTE) * 1000
  GROUP BY policy_decision;
"
```

If `deny` count spiked after a recent policy change, proceed.

## 2. Identify the last known-good policy commit

```bash
# In the policies directory
git log --oneline policies/harness.rego | head -10
```

Or via the OPA API:

```bash
curl http://localhost:8181/v1/policies | python3 -m json.tool | grep -A5 harness
```

## 3. Roll back to the previous policy

**Option A — Git revert and reload:**

```bash
# Revert the bad commit
git revert <BAD_COMMIT_SHA> --no-edit
git push

# Reload OPA (policy files are volume-mounted; OPA hot-reloads on file change)
# If hot-reload is not working, restart OPA:
docker compose restart opa
```

**Option B — Direct PUT to OPA (immediate, no restart needed):**

```bash
# Get the last good policy text
git show <GOOD_COMMIT_SHA>:policies/harness.rego > /tmp/good_policy.rego

# Push it directly to the running OPA instance
curl -X PUT http://localhost:8181/v1/policies/harness \
  -H "Content-Type: text/plain" \
  --data-binary @/tmp/good_policy.rego
```

## 4. Verify the rollback

```bash
# Test a known-good architect tool call
curl -sf -X POST http://localhost:8090/api/v0/tools/invoke \
  -H "Authorization: Bearer $(./scripts/get_token.sh architect)" \
  -H "Content-Type: application/json" \
  -d '{"name":"architect_stub__codebase_search","query":"test"}' | python3 -m json.tool
```

Expected: `200 OK` with tool result.

```bash
# Confirm deny count has dropped
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT policy_decision, COUNT(*) FROM audit_log
  WHERE timestamp_ms > UNIX_TIMESTAMP(NOW() - INTERVAL 1 MINUTE) * 1000
  GROUP BY policy_decision;
"
```

## 5. ContextForge backend rollback

If `GATEWAY_BACKEND=contextforge` is set and the issue is in ContextForge rather
than OPA:

```bash
# Switch governance back to MCPJungle
docker compose up -d --no-deps \
  -e GATEWAY_BACKEND=mcpjungle governance

# Verify Phase 1 tools still work
uv run pytest packages/harness-tests/test_phase1_governance.py -q
```

## 6. Post-incident

- Always test policy changes in a staging environment before pushing to production.
- Add a regression test for any policy that caused an outage.
- Document the change in `docs/adr/` if it was a deliberate security tightening.
