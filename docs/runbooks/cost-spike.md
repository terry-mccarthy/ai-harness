# Runbook: Cost Spike / Runaway Thread

**When to use:** Grafana cost dashboard shows a thread or agent role consuming
significantly more tokens than expected (alert threshold: 2× expected budget).

---

## 1. Identify the runaway thread

**From Grafana:** The cost dashboard shows `token.count` sum grouped by `agent_role`
and `thread_id`. Click the spike to get the `thread_id`.

**From OTel directly:**

```bash
# Query Tempo / Jaeger for spans with high token counts
# (exact query depends on your OTel backend)
# Look for spans named after agent roles with large llm.tokens_used values
```

**From Dolt audit log:**

```bash
mysql -h $DOLT_HOST -P 3306 -u root -proot harness -e "
  SELECT agent_id, COUNT(*) as calls, MAX(timestamp_ms) as last_seen
  FROM audit_log
  WHERE timestamp_ms > UNIX_TIMESTAMP(NOW() - INTERVAL 10 MINUTE) * 1000
  GROUP BY agent_id
  ORDER BY calls DESC
  LIMIT 10;
"
```

## 2. Terminate the runaway thread

The token budget (`token_budget` in `HarnessState`) is the primary governor.
If it was set too high or not set, force-abandon the thread:

```python
import asyncio, os
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def terminate(thread_id: str):
    saver = AsyncPostgresSaver.from_conn_string(os.environ['PG_DSN'])
    config = {"configurable": {"thread_id": thread_id}}
    state = (await saver.aget(config)) or {}
    state["token_budget"] = 0          # force budget_exceeded on next node
    state["tokens_used"] = 999_999
    state["error"] = {"code": "budget_exceeded", "reason": "manual termination"}
    state["final_response"] = "Error: terminated by oncall"
    await saver.aput(config, state, {})
    print(f"Thread {thread_id} terminated.")

asyncio.run(terminate("<THREAD_ID>"))
```

## 3. Rate-limit the offending agent

If a single agent role is generating the spike (e.g., `architect` in a loop):

```bash
# Temporarily lower the rate limit for that role in Redis
# The current rate limiter uses a per-agent per-minute bucket key:
# rl:<agent_sub>:<minute_bucket>
# Set a very high count to force immediate 429:
redis-cli -u $REDIS_URL SET "rl:<AGENT_SUB>:$(date +%s | awk '{print int($1/60)}')" 9999 EX 120
```

## 4. Root-cause investigation

Once the immediate spike is contained:

```bash
# Replay the OTel trace for the thread
# In Grafana Tempo: search by thread_id label
# Look for: classify → formula_lookup → agent → (loop?) → agent → ...

# Check if the graph was cycling (same node appearing multiple times)
# This suggests a conditional edge bug or a formula with infinite steps
```

Common causes:
- Formula with no termination condition (fix: add `max_steps` to formula schema)
- `_should_propose_formula` loop: agent outputs error → error_handler → retried
- Classify/route mis-routing the same task repeatedly

## 5. Adjust defaults

After root-cause is confirmed, update the default token budget in the supervisor:

```python
# In packages/harness-supervisor/harness_supervisor/graph.py
# Set DEFAULT_TOKEN_BUDGET per role:
DEFAULT_BUDGETS = {
    "architect": 4096,
    "code_reviewer": 2048,
    "sre": 3072,
}
```

## 6. Post-incident

- File a bug with the thread_id, agent role, and OTel trace link.
- Verify the budget alert threshold in Grafana is set correctly.
- Consider adding a `max_agent_steps` guard in the graph builder.
