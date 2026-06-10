# Runbook: Agent Unresponsive

**When to use:** A task thread has not produced a `final_response` and appears
stuck — no new OTel spans, no log activity, wall-clock time exceeds the agent's
latency budget (design: 120s, review: 60s, incident: 90s).

---

## 1. Identify the stuck thread

```bash
# Find threads with no final_response in the last 5 minutes
psql $PG_DSN -c "
  SELECT thread_id, created_at, task
  FROM checkpoints
  WHERE final_response IS NULL
    AND created_at < now() - interval '5 minutes'
  ORDER BY created_at DESC
  LIMIT 10;
"
```

Or via OTel / Grafana: look for spans with no end-time in the last 10 minutes.

## 2. Inspect the checkpoint

```bash
# Read the latest checkpoint state for a thread
psql $PG_DSN -c "
  SELECT thread_id, checkpoint_ns, checkpoint
  FROM checkpoints
  WHERE thread_id = '<THREAD_ID>'
  ORDER BY checkpoint_ns DESC
  LIMIT 1;
" | python3 -c "
import sys, json, pickle, base64
row = sys.stdin.read()
# checkpoint column is pickled bytes; print state keys
"
```

Use the LangGraph SDK directly for richer inspection:

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import asyncio, os

async def inspect(thread_id):
    saver = AsyncPostgresSaver.from_conn_string(os.environ['PG_DSN'])
    config = {"configurable": {"thread_id": thread_id}}
    state = await saver.aget(config)
    print(state)

asyncio.run(inspect("<THREAD_ID>"))
```

## 3. Kill the thread

There is no in-process kill signal for a running LangGraph thread. Options:

**Option A — Let it timeout naturally.** The Ollama provider enforces a 120-second
timeout; the `budget_exceeded` path will fire once the thread resumes.

**Option B — Abandon the checkpoint.** Write an error state into the checkpoint so
the next `ainvoke` on that thread returns immediately:

```python
async def abandon(thread_id, reason="manual-kill"):
    saver = AsyncPostgresSaver.from_conn_string(os.environ['PG_DSN'])
    config = {"configurable": {"thread_id": thread_id}}
    state = await saver.aget(config) or {}
    state["error"] = {"code": "abandoned", "reason": reason}
    state["final_response"] = f"Error: {reason}"
    await saver.aput(config, state, {})
```

**Option C — Delete the checkpoint** (full reset, thread can be retried):

```sql
DELETE FROM checkpoints WHERE thread_id = '<THREAD_ID>';
DELETE FROM checkpoint_writes WHERE thread_id = '<THREAD_ID>';
```

## 4. Resume or retry

After abandoning, the caller can retry by submitting the same task with a fresh
`thread_id`. Pass `human_approval_token` if the original task required it.

## 5. Post-incident

- Check OTel for the last span before the thread went quiet (usually an LLM call or
  a tool call that didn't return).
- If the stuck call was `shell_exec`, verify the approval token TTL hasn't expired
  before the SRE agent received it.
- File a bug if the root cause is a non-timeout hang in the LLM provider.
