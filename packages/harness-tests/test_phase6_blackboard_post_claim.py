"""Phase 6 issue-03 — Blackboard: task_post + task_claim (atomic).

Tests verify:
- task_post creates a pending row + Dolt commit
- task_claim returns the highest-priority task for the caller's role
- task_claim returns {task_id: null} when queue is empty
- task_claim is atomic: N concurrent claimers, M tasks → each claimed exactly once
- lease_expires is set on claimed row
All tests are @pytest.mark.integration.
"""

import json
import os
import threading
import time
import uuid

import httpx
import pymysql
import pytest

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


def cleanup_tasks(task_ids: list[str]):
    if not task_ids:
        return
    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
        conn.commit()


@pytest.fixture
def sre_token():
    return get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))


@pytest.fixture
def architect_token():
    return get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))


# ---------------------------------------------------------------------------
# task_post tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_task_post_creates_pending_row(sre_token):
    """task_post returns {task_id, status: pending} and writes a row to Dolt."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={
            "required_role": "sre",
            "artifact_type": "incident",
            "payload": {"alert": "cpu_high"},
            "priority": 0,
        },
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    task_id = body["task_id"]

    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT status, required_role FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
    assert row is not None, "task row not found in Dolt"
    assert row["status"] == "pending"
    assert row["required_role"] == "sre"

    cleanup_tasks([task_id])


@pytest.mark.integration
def test_task_post_creates_dolt_commit(sre_token):
    """task_post produces a Dolt commit with a human-readable message."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={
            "required_role": "sre",
            "artifact_type": "incident",
            "payload": {"alert": "commit_test"},
        },
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 5")
            messages = [r[0] for r in cur.fetchall()]
    assert any("task_post" in m or task_id[:8] in m for m in messages), (
        f"No task_post commit found. Recent: {messages}"
    )
    cleanup_tasks([task_id])


# ---------------------------------------------------------------------------
# task_claim tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_task_claim_returns_null_when_empty(sre_token):
    """Claiming from an empty queue returns {task_id: null}, no error."""
    # Drain any leftover sre tasks first
    while True:
        r = httpx.post(
            f"{GOVERNANCE_URL}/tasks/claim",
            json={"lease_seconds": 5},
            headers={"Authorization": f"Bearer {sre_token}"},
        )
        assert r.status_code == 200
        if r.json()["task_id"] is None:
            break

    resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 5},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_id"] is None


@pytest.mark.integration
def test_task_claim_returns_task(sre_token):
    """task_claim returns the pending task for the matching role."""
    # post a task
    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "sre", "artifact_type": "incident",
              "payload": {"alert": "claim_test"}},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    task_id = post_resp.json()["task_id"]

    claim_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 30},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert claim_resp.status_code == 200
    body = claim_resp.json()
    assert body["task_id"] == task_id
    assert "payload" in body

    # verify DB row is now 'claimed' and lease_expires set
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT status, lease_expires, claimed_by FROM tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
    assert row["status"] == "claimed"
    assert row["lease_expires"] is not None
    cleanup_tasks([task_id])


@pytest.mark.integration
def test_task_claim_priority_ordering(sre_token):
    """Higher-priority task is claimed before lower-priority one."""
    ids = []
    for priority in [0, 10, 5]:
        r = httpx.post(
            f"{GOVERNANCE_URL}/tasks",
            json={"required_role": "sre", "artifact_type": "incident",
                  "payload": {"priority_val": priority}, "priority": priority},
            headers={"Authorization": f"Bearer {sre_token}"},
        )
        ids.append(r.json()["task_id"])

    # Claim — should get priority=10 first
    resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 30},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    claimed = resp.json()
    assert claimed["task_id"] is not None
    payload = claimed["payload"]
    assert payload.get("priority_val") == 10, (
        f"Expected highest priority task, got payload: {payload}"
    )
    cleanup_tasks(ids)


@pytest.mark.integration
def test_task_claim_role_isolation(sre_token, architect_token):
    """sre worker cannot claim an architect task."""
    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "architect", "artifact_type": "design",
              "payload": {"decision": "isolation test"}},
        headers={"Authorization": f"Bearer {architect_token}"},
    )
    arch_task_id = post_resp.json()["task_id"]

    # sre should not get the architect task
    claim_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 5},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert claim_resp.status_code == 200
    assert claim_resp.json()["task_id"] != arch_task_id

    cleanup_tasks([arch_task_id])


@pytest.mark.integration
def test_task_claim_atomic_no_double_grab():
    """N concurrent claimers vs M tasks — each task claimed exactly once."""
    M = 5   # tasks
    N = 10  # claimers (> M)

    sre_token = get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))

    # Post M tasks
    task_ids = []
    for i in range(M):
        r = httpx.post(
            f"{GOVERNANCE_URL}/tasks",
            json={"required_role": "sre", "artifact_type": "incident",
                  "payload": {"seq": i}, "priority": 0},
            headers={"Authorization": f"Bearer {sre_token}"},
        )
        assert r.status_code == 200
        task_ids.append(r.json()["task_id"])

    results = []
    lock = threading.Lock()

    def claim_once():
        tok = get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
        r = httpx.post(
            f"{GOVERNANCE_URL}/tasks/claim",
            json={"lease_seconds": 60},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10.0,
        )
        with lock:
            if r.status_code == 200:
                results.append(r.json()["task_id"])

    threads = [threading.Thread(target=claim_once) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_non_null = [tid for tid in results if tid is not None]
    assert len(claimed_non_null) == M, (
        f"Expected {M} unique claims, got {len(claimed_non_null)}: {claimed_non_null}"
    )
    assert len(set(claimed_non_null)) == M, (
        f"Duplicate claims detected: {claimed_non_null}"
    )

    cleanup_tasks(task_ids)
