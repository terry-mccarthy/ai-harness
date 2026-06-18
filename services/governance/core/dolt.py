"""Dolt MySQL connection and audit/episode write helpers."""
import json
import logging
import time
import uuid

import pymysql

from .config import DOLT_DB, DOLT_HOST, DOLT_PASSWORD, DOLT_PORT, DOLT_USER

logger = logging.getLogger(__name__)


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=DOLT_USER,
        password=DOLT_PASSWORD,
        database=DOLT_DB,
        autocommit=True,
    )


def write_episode(
    agent_principal, tool_name, short_tool, req_hash, correlation_id, service_class,
):
    conn = None
    try:
        episode_id = str(uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        alert_sig = f"{agent_principal}.{short_tool}:{correlation_id or ''}"
        env_fp = json.dumps({"tool_name": tool_name, "server_id": short_tool, "timestamp_ms": timestamp_ms})
        actions = json.dumps([{"tool": tool_name, "scoped_args": req_hash, "scope_token_ref": correlation_id}])
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes "
                "(episode_id, agent_principal, alert_signature, service_class, env_fingerprint, actions) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (episode_id, agent_principal, alert_sig, service_class or "unknown", env_fp, actions),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"episode: {short_tool} by {agent_principal}",),
            )
    except Exception as e:
        logger.error("Dolt episode write failed: %s", e)
    finally:
        if conn:
            conn.close()


def write_audit(
    agent_id, tool_name, server_id, req_hash, resp_hash,
    decision, rule, latency_ms, correlation_id=None,
):
    conn = None
    try:
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
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
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"audit: {tool_name} by {agent_id} [{decision}]",),
            )
    except Exception as e:
        logger.error("Dolt audit write failed: %s", e)
    finally:
        if conn:
            conn.close()


def write_gate_failure(
    thread_id, rule, severity, file, message, task, repo_path, target_language, gate_signal,
):
    conn = None
    try:
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
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
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"gate_failure: {rule} [{severity}]",),
            )
    except Exception as e:
        logger.error("Dolt gate failure write failed: %s", e)
    finally:
        if conn:
            conn.close()


def serialise_row(row: dict) -> dict:
    """Convert datetime and bytes values in a Dolt row to JSON-safe types."""
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else v.decode() if isinstance(v, (bytes, bytearray)) else v)
        for k, v in row.items()
    }
