"""Phase 6 issue-07 — Correlation ID threading.

Tests verify:
- audit_log has a correlation_id column (nullable)
- a multi-step workflow threads the same correlation_id through all audit rows
- single-step tool calls produce an audit row; correlation_id may be null
- denied invocations include correlation_id when one is provided
All tests are @pytest.mark.integration.
"""

import os
import time
import uuid
import pytest
import httpx
import pymysql

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


def get_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5,
    )


@pytest.fixture
def architect_token():
    return get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))


@pytest.fixture
def reviewer_token():
    return get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_audit_log_has_correlation_id_column():
    """audit_log has a nullable correlation_id column."""
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("DESCRIBE audit_log")
            cols = {row["Field"]: row for row in cur.fetchall()}
    assert "correlation_id" in cols, (
        f"audit_log missing correlation_id column; columns: {list(cols)}"
    )
    assert cols["correlation_id"]["Null"] == "YES", "correlation_id must be nullable"


# ---------------------------------------------------------------------------
# Threading through a multi-step chain
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_correlation_id_threads_chain(architect_token):
    """Two sequential agent_invoke calls with the same correlation_id share it in audit_log."""
    correlation_id = str(uuid.uuid4())
    before_ts = int(time.time() * 1000)

    # First hop: architect → code-reviewer
    resp1 = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"repo": "test", "base_ref": "HEAD~1", "head_ref": "HEAD"},
        },
        headers={
            "Authorization": f"Bearer {architect_token}",
            "X-Correlation-Id": correlation_id,
        },
        timeout=60.0,
    )
    assert resp1.status_code == 200, resp1.text

    # Second hop: architect → code-reviewer (simulated second step)
    resp2 = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"repo": "test", "base_ref": "HEAD~2", "head_ref": "HEAD~1"},
        },
        headers={
            "Authorization": f"Bearer {architect_token}",
            "X-Correlation-Id": correlation_id,
        },
        timeout=60.0,
    )
    assert resp2.status_code == 200, resp2.text

    time.sleep(0.5)

    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT correlation_id, agent_id, timestamp_ms FROM audit_log "
                "WHERE correlation_id = %s AND timestamp_ms >= %s "
                "ORDER BY timestamp_ms ASC",
                (correlation_id, before_ts),
            )
            rows = cur.fetchall()

    assert len(rows) >= 2, (
        f"Expected ≥2 audit rows with correlation_id={correlation_id}, got {len(rows)}"
    )
    for row in rows:
        assert row["correlation_id"] == correlation_id, (
            f"Row has wrong correlation_id: {row}"
        )


@pytest.mark.integration
def test_correlation_id_in_denied_invocation(reviewer_token):
    """Denied invocations include correlation_id when provided."""
    correlation_id = str(uuid.uuid4())
    before_ts = int(time.time() * 1000)

    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "sre",
            "artifact_type": "incident",
            "payload": {"alert": "inject"},
        },
        headers={
            "Authorization": f"Bearer {reviewer_token}",
            "X-Correlation-Id": correlation_id,
        },
    )
    assert resp.status_code == 403

    time.sleep(0.5)
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT correlation_id, policy_decision FROM audit_log "
                "WHERE correlation_id = %s AND policy_decision = 'deny' "
                "AND timestamp_ms >= %s",
                (correlation_id, before_ts),
            )
            rows = cur.fetchall()
    assert len(rows) >= 1, (
        f"Expected deny audit row with correlation_id={correlation_id}, got: {rows}"
    )


@pytest.mark.integration
def test_single_step_audit_row_null_correlation():
    """A plain /audit call (no correlation_id) writes a row with null correlation_id."""
    architect_token = get_token(
        "architect", os.environ.get("ARCHITECT_SECRET", "architect-secret")
    )
    before_ts = int(time.time() * 1000)

    httpx.post(
        f"{GOVERNANCE_URL}/audit",
        json={
            "tool_name": "architect_stub__codebase_search",
            "decision": "allow",
            "latency_ms": 10,
        },
        headers={"Authorization": f"Bearer {architect_token}"},
    )

    time.sleep(0.5)
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT correlation_id FROM audit_log "
                "WHERE agent_id = 'architect' AND timestamp_ms >= %s "
                "ORDER BY timestamp_ms DESC LIMIT 1",
                (before_ts,),
            )
            row = cur.fetchone()
    assert row is not None, "audit row not found"
    assert row["correlation_id"] is None, (
        f"Expected null correlation_id for plain audit, got: {row['correlation_id']}"
    )
