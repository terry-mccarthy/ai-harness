# Runbook: Bad Formula Rollback

**When to use:** A formula in the Dolt formula store is causing agent failures —
wrong steps, bad output contract, or a regression introduced by a quality-score
graduation that shouldn't have happened.

---

## 1. Identify the bad formula

**From agent errors:**

```bash
# Find formulas used in recent failing runs
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT f.id, f.name, f.agent_role, f.status, f.quality_score, f.pour_count, f.failure_count
  FROM formula_store f
  WHERE f.failure_count > 0
  ORDER BY f.failure_count DESC
  LIMIT 10;
"
```

**From Dolt log (find the commit that changed the formula):**

```bash
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT commit_hash, committer_name, message, date
  FROM dolt_log
  ORDER BY date DESC
  LIMIT 20;
"
```

## 2. Inspect the formula diff

```bash
# See what changed in the formula since the last known-good commit
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT *
  FROM dolt_diff_formula_store
  WHERE to_id = '<FORMULA_ID>'
  LIMIT 5;
"
```

Or use the Dolt CLI (in the dolt container):

```bash
docker exec friday-dolt-1 dolt diff <GOOD_COMMIT>..<BAD_COMMIT> formula_store
```

## 3. Revert the formula commit

**Option A — Dolt revert a single commit:**

```bash
docker exec friday-dolt-1 bash -c "
  cd /doltdata && \
  dolt revert <BAD_COMMIT_SHA> && \
  dolt commit -m 'revert: bad formula <FORMULA_ID>'
"
```

**Option B — Direct SQL update (mark formula deprecated immediately):**

```python
import pymysql, os

conn = pymysql.connect(
    host=os.environ.get('DOLT_HOST', 'localhost'),
    port=3306, user='root', password='root', database='harness', autocommit=True
)
with conn.cursor() as cur:
    cur.execute(
        "UPDATE formula_store SET status='deprecated' WHERE id = %s",
        ('<FORMULA_ID>',)
    )
    cur.execute(
        "CALL DOLT_COMMIT('-Am', 'fix: deprecate bad formula <FORMULA_ID>')"
    )
conn.close()
print("Formula deprecated.")
```

**Option C — Reset to a specific commit (destructive, use with care):**

```bash
docker exec friday-dolt-1 bash -c "
  cd /doltdata && \
  dolt reset --hard <GOOD_COMMIT_SHA>
"
```

## 4. Verify the rollback

```bash
# Confirm the formula is now deprecated (or reverted)
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT id, status, quality_score FROM formula_store WHERE id = '<FORMULA_ID>';
"

# Confirm dolt log shows the revert commit
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT commit_hash, message, date FROM dolt_log ORDER BY date DESC LIMIT 3;
"
```

## 5. Replay to verify agents recover

```bash
# Submit a representative task and confirm it runs without the bad formula
uv run python -c "
import asyncio, os
from harness_supervisor.graph import build_supervisor
# ... submit the task type that was affected
"
```

`formula_lookup` returns `None` for deprecated formulas, so agents fall back to
ad-hoc LLM execution automatically.

## 6. Post-incident

- Add a regression test: a formula with the bad step structure should fail validation
  before being written to the store.
- Consider adding a `staging` status (formula works in test but not prod) to the
  formula lifecycle state machine.
- Review quality-score graduation thresholds (`prove_threshold` in formula store) —
  if a formula graduated too quickly, lower the pour count requirement.
