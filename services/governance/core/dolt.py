"""Dolt MySQL connection and audit/episode write helpers."""
import asyncio
import json
import logging
import time
import uuid

import aiomysql
import pymysql

from .config import DOLT_DB, DOLT_HOST, DOLT_PASSWORD, DOLT_PORT, DOLT_USER

logger = logging.getLogger(__name__)

# Shared async connection pool used by write_episode/write_audit/write_gate_failure.
# Created lazily on first use (or eagerly via init_pool() at app startup) and
# reused across requests instead of opening a new connection per call.
_pool: aiomysql.Pool | None = None
_pool_lock = asyncio.Lock()


def get_dolt_conn():
    """Synchronous pymysql connection — used by routers doing raw sync reads
    (tasks.py, skills.py, learning.py). Left untouched by the async pooling
    work in this module; those callers are out of scope for issue #03."""
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=DOLT_USER,
        password=DOLT_PASSWORD,
        database=DOLT_DB,
        autocommit=True,
    )


async def init_pool() -> aiomysql.Pool:
    """Create the shared async pool if it doesn't exist yet, and return it.

    Safe to call repeatedly (e.g. once eagerly at FastAPI startup, and lazily
    from the write_* helpers) — only the first caller pays the connect cost.
    """
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await aiomysql.create_pool(
                    host=DOLT_HOST,
                    port=DOLT_PORT,
                    user=DOLT_USER,
                    password=DOLT_PASSWORD,
                    db=DOLT_DB,
                    autocommit=True,
                    minsize=1,
                    maxsize=10,
                )
    return _pool


async def close_pool() -> None:
    """Close the shared pool (called on FastAPI shutdown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def write_episode(
    agent_principal, tool_name, short_tool, req_hash, correlation_id, service_class,
):
    try:
        episode_id = str(uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        alert_sig = f"{agent_principal}.{short_tool}:{correlation_id or ''}"
        env_fp = json.dumps({"tool_name": tool_name, "server_id": short_tool, "timestamp_ms": timestamp_ms})
        actions = json.dumps([{"tool": tool_name, "scoped_args": req_hash, "scope_token_ref": correlation_id}])
        pool = await init_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO episodes "
                    "(episode_id, agent_principal, alert_signature, service_class, env_fingerprint, actions) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (episode_id, agent_principal, alert_sig, service_class or "unknown", env_fp, actions),
                )
                await cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"episode: {short_tool} by {agent_principal}",),
                )
    except Exception as e:
        logger.error("Dolt episode write failed: %s", e)


async def write_audit(
    agent_id, tool_name, server_id, req_hash, resp_hash,
    decision, rule, latency_ms, correlation_id=None,
):
    try:
        pool = await init_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO audit_log
                       (agent_id, tool_name, server_id, request_hash, response_hash,
                        policy_decision, policy_rule, timestamp_ms, latency_ms, correlation_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        agent_id,
                        tool_name,
                        server_id,
                        req_hash,
                        resp_hash,
                        decision,
                        rule,
                        int(time.time() * 1000),
                        latency_ms,
                        correlation_id,
                    ),
                )
                await cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"audit: {tool_name} by {agent_id} [{decision}]",),
                )
    except Exception as e:
        logger.error("Dolt audit write failed: %s", e)


async def write_gate_failure(
    thread_id, rule, severity, file, message, task, repo_path, target_language, gate_signal,
):
    try:
        pool = await init_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO architectural_gate_failures
                       (thread_id, rule, severity, file, message, task, repo_path,
                        target_language, gate_signal, timestamp_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        thread_id,
                        rule,
                        severity,
                        file,
                        message,
                        task,
                        repo_path,
                        target_language,
                        json.dumps(gate_signal) if isinstance(gate_signal, dict) else gate_signal,
                        int(time.time() * 1000),
                    ),
                )
                await cur.execute(
                    "CALL DOLT_COMMIT('-Am', %s)",
                    (f"gate_failure: {rule} [{severity}]",),
                )
    except Exception as e:
        logger.error("Dolt gate failure write failed: %s", e)


def serialise_row(row: dict) -> dict:
    """Convert datetime and bytes values in a Dolt row to JSON-safe types."""
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else v.decode() if isinstance(v, (bytes, bytearray)) else v)
        for k, v in row.items()
    }
