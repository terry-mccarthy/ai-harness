"""Skill conflict resolution and escalation — issue 08.

Tests:
- POST /skills/select returns the most specific matching ACTIVE skill
- Tied specificity resolves by promotion recency
- Tied recency resolves by trailing success rate
- Full tie returns escalate: true with tied skill IDs and scores
- Every selection (win or escalate) is written to audit_log with tool_name='skill:select'
- Unauthenticated requests return 401
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))

pytestmark = pytest.mark.integration


def _sre_token() -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "sre",
            "client_secret": os.environ.get("SRE_SECRET", "sre-secret"),
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user="root",
        password="root",
        database="harness",
        connect_timeout=5,
        autocommit=True,
    )


def _insert_skill(
    agent_role: str,
    preconditions: dict | None = None,
    created_at: datetime | None = None,
) -> str:
    skill_id = f"test:{uuid.uuid4().hex[:10]}"
    ts = (created_at or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    prec_json = json.dumps(preconditions) if preconditions is not None else None
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skills "
                "(id, name, agent_role, version, status, input_schema, steps, output_contract, "
                "promoted_by, expires_at, preconditions, created_at) "
                "VALUES (%s, %s, %s, 1, 'active', '{}', '[]', '{}', 'test-runner', "
                "DATE_ADD(NOW(), INTERVAL 365 DAY), %s, %s)",
                (skill_id, skill_id, agent_role, prec_json, ts),
            )
    return skill_id


def _insert_audit_row(agent_id: str, decision: str) -> None:
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (agent_id, tool_name, policy_decision, timestamp_ms) "
                "VALUES (%s, 'some_tool', %s, %s)",
                (agent_id, decision, int(time.time() * 1000)),
            )


def _call_select(token: str, env_fingerprint: dict | None = None) -> httpx.Response:
    return httpx.post(
        f"{GOVERNANCE_URL}/skills/select",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "alert_signature": "sre.observability_query:latency-spike",
            "service_class": "stateless-api",
            "env_fingerprint": env_fingerprint or {},
            "invoking_principal": "sre-agent-1",
        },
        timeout=10.0,
    )


def _audit_select_entries_since(before_ms: int, caller_sub: str = "sre") -> list[dict]:
    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM audit_log WHERE tool_name='skill:select' "
                "AND agent_id=%s AND timestamp_ms >= %s",
                (caller_sub, before_ms),
            )
            return cur.fetchall() or []


def test_select_most_specific_wins():
    unique = uuid.uuid4().hex[:8]
    fp = {f"k_{unique}": "v1", f"k2_{unique}": "v2"}
    # 0 constraints matched (NULL preconditions)
    _insert_skill(agent_role=f"role_A_{unique}")
    # 1 constraint matched
    _insert_skill(
        agent_role=f"role_B_{unique}",
        preconditions={"env_constraints": {f"k_{unique}": "v1"}},
    )
    # 2 constraints matched — should win
    id_C = _insert_skill(
        agent_role=f"role_C_{unique}",
        preconditions={"env_constraints": fp},
    )

    resp = _call_select(_sre_token(), env_fingerprint=fp)
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"] == id_C
    assert body["rationale"]["rule"] == "specificity"
    assert body["rationale"]["score"] == 2


def test_select_recency_tiebreak():
    unique = uuid.uuid4().hex[:8]
    fp = {f"rk_{unique}": "v1"}
    prec = {"env_constraints": fp}
    older_ts = datetime.utcnow() - timedelta(hours=2)
    newer_ts = datetime.utcnow() - timedelta(hours=1)
    _insert_skill(agent_role=f"role_old_{unique}", preconditions=prec, created_at=older_ts)
    id_new = _insert_skill(agent_role=f"role_new_{unique}", preconditions=prec, created_at=newer_ts)

    resp = _call_select(_sre_token(), env_fingerprint=fp)
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"] == id_new
    assert body["rationale"]["rule"] == "recency"


def test_select_success_rate_tiebreak():
    unique = uuid.uuid4().hex[:8]
    fp = {f"srk_{unique}": "v1"}
    prec = {"env_constraints": fp}
    ts = (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0)
    role_low = f"low_{unique}"
    role_high = f"high_{unique}"
    _insert_skill(agent_role=role_low, preconditions=prec, created_at=ts)
    id_high = _insert_skill(agent_role=role_high, preconditions=prec, created_at=ts)
    for _ in range(3):
        _insert_audit_row(role_low, "deny")
    for _ in range(3):
        _insert_audit_row(role_high, "allow")

    resp = _call_select(_sre_token(), env_fingerprint=fp)
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"] == id_high
    assert body["rationale"]["rule"] == "success_rate"


def test_select_full_tie_escalates():
    unique = uuid.uuid4().hex[:8]
    fp = {f"ek_{unique}": "v1"}
    prec = {"env_constraints": fp}
    ts = (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0)
    id_A = _insert_skill(agent_role=f"tied_A_{unique}", preconditions=prec, created_at=ts)
    id_B = _insert_skill(agent_role=f"tied_B_{unique}", preconditions=prec, created_at=ts)

    resp = _call_select(_sre_token(), env_fingerprint=fp)
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"] is None
    assert body["escalate"] is True
    assert "reason" in body
    tied_ids = [t["id"] for t in body["tied_skills"]]
    assert id_A in tied_ids
    assert id_B in tied_ids


def test_select_win_logs_to_audit_log():
    unique = uuid.uuid4().hex[:8]
    fp = {f"lk_{unique}": "v1"}
    _insert_skill(
        agent_role=f"role_log_{unique}",
        preconditions={"env_constraints": fp},
    )

    before_ms = int(time.time() * 1000)
    _call_select(_sre_token(), env_fingerprint=fp)
    time.sleep(1.0)

    entries = _audit_select_entries_since(before_ms, caller_sub="sre")
    assert len(entries) >= 1
    assert entries[0]["tool_name"] == "skill:select"
    assert entries[0]["policy_decision"] == "allow"


def test_select_escalation_logs_to_audit_log():
    unique = uuid.uuid4().hex[:8]
    fp = {f"elk_{unique}": "v1"}
    prec = {"env_constraints": fp}
    ts = (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0)
    _insert_skill(agent_role=f"esc_A_{unique}", preconditions=prec, created_at=ts)
    _insert_skill(agent_role=f"esc_B_{unique}", preconditions=prec, created_at=ts)

    before_ms = int(time.time() * 1000)
    _call_select(_sre_token(), env_fingerprint=fp)
    time.sleep(1.0)

    entries = _audit_select_entries_since(before_ms, caller_sub="sre")
    assert len(entries) >= 1
    assert entries[0]["tool_name"] == "skill:select"


def test_select_requires_auth():
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/select",
        json={"env_fingerprint": {}},
        timeout=10.0,
    )
    assert resp.status_code == 401
