"""Phase 1 Governance tests — all @pytest.mark.integration.

Governance is now a policy+audit sidecar (no forwarding proxy):
  - POST /oauth/token   — issues role-bearing JWTs
  - POST /check         — OPA policy decision
  - POST /audit         — async Dolt audit write
"""

import os
import time
import pytest
import httpx
import pymysql
import jwt

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")

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


def check_policy(token: str, tool_name: str) -> httpx.Response:
    """POST /check — returns 200 (allowed) or 403 (denied) or 401 (bad token)."""
    return httpx.post(
        f"{GOVERNANCE_URL}/check",
        json={"tool_name": tool_name},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def post_audit(token: str, tool_name: str) -> httpx.Response:
    """POST /audit — records a tool call in Dolt (async, returns 202)."""
    return httpx.post(
        f"{GOVERNANCE_URL}/audit",
        json={"tool_name": tool_name, "decision": "allow", "latency_ms": 50},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user="harness",
        password="harness",
        database="harness",
        autocommit=True,
    )


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_architect_client_auth():
    """POST /oauth/token with architect credentials returns access_token."""
    architect_secret = os.environ.get("ARCHITECT_SECRET", "architect-secret")
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "architect",
            "client_secret": architect_secret,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data.get("token_type", "").lower() == "bearer"


@pytest.mark.integration
def test_reviewer_client_auth():
    """POST /oauth/token with code-reviewer credentials returns access_token."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "code-reviewer",
            "client_secret": reviewer_secret,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


@pytest.mark.integration
def test_sre_client_auth():
    """POST /oauth/token with sre credentials returns access_token."""
    sre_secret = os.environ.get("SRE_SECRET", "sre-secret")
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "sre",
            "client_secret": sre_secret,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


# ---------------------------------------------------------------------------
# Policy check tests (POST /check)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_architect_allowed_tool():
    """Architect token gets allowed=true for codebase_search."""
    token = get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))
    resp = check_policy(token, "architect_stub__codebase_search")
    assert resp.status_code == 200
    assert resp.json().get("allowed") is True


@pytest.mark.integration
def test_architect_denied_tool():
    """Architect token calling shell_exec returns 403."""
    token = get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))
    resp = check_policy(token, "sre_stub__shell_exec")
    assert resp.status_code == 403


@pytest.mark.integration
def test_reviewer_allowed_tool():
    """code-reviewer token gets allowed=true for git_diff."""
    token = get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    resp = check_policy(token, "git_diff_stub__git_diff")
    assert resp.status_code == 200
    assert resp.json().get("allowed") is True


@pytest.mark.integration
def test_reviewer_denied_tool():
    """code-reviewer token calling adr_write returns 403."""
    token = get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    resp = check_policy(token, "architect_stub__adr_write")
    assert resp.status_code == 403


@pytest.mark.integration
def test_sre_allowed_tool():
    """sre token gets allowed=true for runbook_read."""
    token = get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = check_policy(token, "sre_stub__runbook_read")
    assert resp.status_code == 200
    assert resp.json().get("allowed") is True


@pytest.mark.integration
def test_unknown_token_rejected():
    """Request with 'Bearer invalid-token' returns 401 from /check."""
    resp = check_policy("invalid-token", "git_diff_stub__git_diff")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Audit / Dolt tests (POST /audit → Dolt)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_row_written():
    """After POST /audit, a row exists in audit_log table (Dolt via MySQL)."""
    token = get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    audit_resp = post_audit(token, "git_diff_stub__git_diff")
    assert audit_resp.status_code == 202

    # Brief wait for background write
    time.sleep(1)

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM audit_log")
            (count,) = cur.fetchone()
        assert count > 0, "No audit rows found in audit_log"
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_policy_rule_recorded():
    """audit_log row has policy_decision and policy_rule fields populated."""
    token = get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    post_audit(token, "git_diff_stub__git_diff")
    time.sleep(1)

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT policy_decision, policy_rule FROM audit_log "
                "ORDER BY timestamp_ms DESC LIMIT 1"
            )
            row = cur.fetchone()
        assert row is not None, "No audit rows found"
        policy_decision, policy_rule = row
        assert policy_decision in ("allow", "deny")
        assert policy_rule is not None and len(policy_rule) > 0
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_dolt_commit_created():
    """After /audit, DOLT_LOG() shows a new commit with the tool name."""
    token = get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    post_audit(token, "git_diff_stub__git_diff")
    time.sleep(1)

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 5")
            rows = cur.fetchall()
        messages = [r[0] for r in rows]
        assert any("audit:" in msg for msg in messages), (
            f"No audit commit found in dolt_log. Messages: {messages}"
        )
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_dolt_history_queryable():
    """SELECT * FROM dolt_diff_audit_log returns at least one row after a commit."""
    token = get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])
    post_audit(token, "git_diff_stub__git_diff")
    time.sleep(1)

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dolt_diff_audit_log LIMIT 5")
            (count,) = cur.fetchone()
        assert count > 0, "dolt_diff_audit_log returned no rows"
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_no_delete():
    """DELETE FROM audit_log raises an error (harness user has INSERT only, no DELETE)."""
    conn = get_dolt_conn()
    try:
        with pytest.raises(Exception, match="DELETE|command denied|1142"):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE 1=0")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OPA unit tests (direct HTTP to OPA)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_opa_allow_architect_tool():
    """OPA POST /v1/data/harness/allow with {architect, codebase_search} returns true."""
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/allow",
        json={"input": {"agent_role": "architect", "tool_name": "codebase_search"}},
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json().get("result") is True


@pytest.mark.integration
def test_opa_deny_cross_role():
    """OPA POST /v1/data/harness/allow with {architect, shell_exec} returns false."""
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/allow",
        json={"input": {"agent_role": "architect", "tool_name": "shell_exec"}},
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json().get("result") is not True


# ---------------------------------------------------------------------------
# Token expiry test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_token_expiry():
    """Forge a JWT with exp in the past using the test private key; governance /check returns 401."""
    import pathlib
    key_path = pathlib.Path(__file__).parent.parent.parent / "test-fixtures" / "jwt-test-key.pem"
    private_key = key_path.read_bytes()
    expired = jwt.encode(
        {
            "sub": "code-reviewer",
            "role": "code_reviewer",
            "iat": int(time.time()) - 1000,
            "exp": int(time.time()) - 100,
        },
        private_key,
        algorithm="RS256",
    )
    resp = check_policy(expired, "git_diff_stub__git_diff")
    assert resp.status_code == 401
