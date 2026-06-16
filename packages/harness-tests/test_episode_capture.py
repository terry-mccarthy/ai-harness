"""Episode capture via POST /audit — issue 02.

Verifies that each /audit call writes a row to the episodes table alongside
the existing audit_log write. The two writes are independent background tasks.
"""

import os
import time
import uuid

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


def _get_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _harness_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="harness", password="harness",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _post_audit(token: str, tool_name: str, correlation_id: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id
    return httpx.post(
        f"{GOVERNANCE_URL}/audit",
        json={"tool_name": tool_name, "decision": "allow", "latency_ms": 20},
        headers=headers,
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Episode creation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_writes_episode_row():
    """POST /audit creates a row in episodes with outcome=NULL."""
    correlation_id = str(uuid.uuid4())
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = _post_audit(token, "sre_stub__observability_query", correlation_id)
    assert resp.status_code == 202

    time.sleep(1)

    conn = _harness_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT agent_principal, alert_signature, outcome FROM episodes "
                "WHERE alert_signature LIKE %s ORDER BY created_at DESC LIMIT 1",
                (f"%{correlation_id}%",),
            )
            row = cur.fetchone()

    assert row is not None, f"No episode row found for correlation_id={correlation_id}"
    assert row["outcome"] is None, "outcome must be NULL at capture time"


@pytest.mark.integration
def test_episode_agent_principal_matches_jwt_sub():
    """agent_principal is populated from the JWT sub claim."""
    correlation_id = str(uuid.uuid4())
    token = _get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))
    resp = _post_audit(token, "architect_stub__codebase_search", correlation_id)
    assert resp.status_code == 202

    time.sleep(1)

    conn = _harness_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT agent_principal FROM episodes WHERE alert_signature LIKE %s LIMIT 1",
                (f"%{correlation_id}%",),
            )
            row = cur.fetchone()

    assert row is not None, f"No episode row for correlation_id={correlation_id}"
    assert row["agent_principal"] == "architect"


# ---------------------------------------------------------------------------
# Independence — episode failure must not break the 202
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_still_returns_202():
    """POST /audit always returns 202 (episode write is fire-and-forget)."""
    token = _get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    resp = _post_audit(token, "git_diff_stub__git_diff")
    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Existing audit_log write is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_log_still_written():
    """Existing audit_log write is unaffected after episode capture is added."""
    token = _get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    resp = _post_audit(token, "git_diff_stub__git_diff")
    assert resp.status_code == 202

    time.sleep(1)

    conn = _harness_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM audit_log")
            (count,) = cur.fetchone()
    assert count > 0, "audit_log is empty — existing write was broken"
