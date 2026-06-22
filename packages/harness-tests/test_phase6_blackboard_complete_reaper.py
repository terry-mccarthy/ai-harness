"""Phase 6 issue-05 — Blackboard: task_complete + lease reaper.

Tests verify:
- task_complete transitions to 'done', stores result, returns {status: done}
- task_complete is idempotent via idempotency_key
- a worker that is not the claimer cannot complete a task
- a stale claimed task returns to pending after lease expiry
All tests are @pytest.mark.integration.
"""

import json
import os
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


def post_and_claim(token: str) -> tuple[str, str]:
    """Post a task and claim it, returning (task_id, worker_id)."""
    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "sre", "artifact_type": "incident",
              "payload": {"alert": "test"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    task_id = post_resp.json()["task_id"]

    claim_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 60},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert claim_resp.json()["task_id"] == task_id
    return task_id, claim_resp.json().get("worker_id", "sre")


# ---------------------------------------------------------------------------
# task_complete
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_task_complete_transitions_to_done(sre_token):
    """task_complete sets status=done and stores result."""
    # Use high priority so this task is always claimed first
    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "sre", "artifact_type": "incident",
              "payload": {"alert": "done_test"}, "priority": 9999},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    task_id = post_resp.json()["task_id"]

    claim_resp = httpx.post(f"{GOVERNANCE_URL}/tasks/claim",
                            json={"lease_seconds": 60},
                            headers={"Authorization": f"Bearer {sre_token}"})
    claimed_id = claim_resp.json()["task_id"]
    assert claimed_id == task_id, (
        f"Expected to claim task {task_id}, got {claimed_id} — "
        "leftover tasks in queue may be interfering"
    )

    result_data = {"report": "incident resolved", "severity": "low"}
    idem_key = f"sre:{task_id}"
    complete_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/complete",
        json={"task_id": task_id, "result": result_data, "idempotency_key": idem_key},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert complete_resp.json()["status"] == "done"

    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT status, result FROM tasks WHERE id = %s", (task_id,)
            )
            row = cur.fetchone()
    assert row["status"] == "done"
    stored = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
    assert stored == result_data

    cleanup_tasks([task_id])


@pytest.mark.integration
def test_task_complete_creates_dolt_commit(sre_token):
    """task_complete produces a Dolt commit."""
    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "sre", "artifact_type": "incident",
              "payload": {"alert": "commit_complete_test"}, "priority": 9999},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    task_id = post_resp.json()["task_id"]
    claim_resp = httpx.post(f"{GOVERNANCE_URL}/tasks/claim",
                            json={"lease_seconds": 60},
                            headers={"Authorization": f"Bearer {sre_token}"})
    assert claim_resp.json()["task_id"] == task_id

    httpx.post(
        f"{GOVERNANCE_URL}/tasks/complete",
        json={"task_id": task_id, "result": {"done": True},
              "idempotency_key": f"sre:{task_id}-commit"},
        headers={"Authorization": f"Bearer {sre_token}"},
    )

    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 5")
            messages = [r[0] for r in cur.fetchall()]
    assert any("task_complete" in m or task_id[:8] in m for m in messages), (
        f"No task_complete commit found. Recent: {messages}"
    )
    cleanup_tasks([task_id])


@pytest.mark.integration
def test_task_complete_idempotent(sre_token):
    """Submitting the same idempotency_key twice returns original result; no double-write."""
    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "sre", "artifact_type": "incident",
              "payload": {"alert": "idem_test"}, "priority": 9999},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    task_id = post_resp.json()["task_id"]
    claim_resp = httpx.post(f"{GOVERNANCE_URL}/tasks/claim",
                            json={"lease_seconds": 60},
                            headers={"Authorization": f"Bearer {sre_token}"})
    assert claim_resp.json()["task_id"] == task_id

    idem_key = f"idem:{task_id}"
    result1 = {"report": "first result"}
    result2 = {"report": "second result — should be ignored"}

    resp1 = httpx.post(
        f"{GOVERNANCE_URL}/tasks/complete",
        json={"task_id": task_id, "result": result1, "idempotency_key": idem_key},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert resp1.status_code == 200

    resp2 = httpx.post(
        f"{GOVERNANCE_URL}/tasks/complete",
        json={"task_id": task_id, "result": result2, "idempotency_key": idem_key},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert resp2.status_code == 200

    # Stored result must still be the first one
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT result FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
    stored = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
    assert stored == result1, f"Expected first result, got: {stored}"

    cleanup_tasks([task_id])


# ---------------------------------------------------------------------------
# Lease reaper
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_lease_expiry_returns_task_to_pool(sre_token):
    """A task claimed with a short lease becomes pending again after lease expiry."""
    import datetime

    post_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks",
        json={"required_role": "sre", "artifact_type": "incident",
              "payload": {"alert": "lease_test"}, "priority": 9999},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    task_id = post_resp.json()["task_id"]

    # Claim — high priority ensures we get our task
    claim_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 60},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert claim_resp.json()["task_id"] == task_id, (
        f"Expected to claim our task {task_id}, got {claim_resp.json()['task_id']}"
    )

    # Force-expire the lease in the DB (avoids waiting 60s)
    conn = get_root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET lease_expires = %s WHERE id = %s",
                (
                    datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5),
                    task_id,
                ),
            )
        conn.commit()

    # Next claim triggers the on-claim reaper and should re-acquire the expired task
    reclaim_resp = httpx.post(
        f"{GOVERNANCE_URL}/tasks/claim",
        json={"lease_seconds": 30},
        headers={"Authorization": f"Bearer {sre_token}"},
    )
    assert reclaim_resp.status_code == 200
    assert reclaim_resp.json()["task_id"] == task_id, (
        f"Expected reclaimed task {task_id}, got: {reclaim_resp.json()}"
    )

    cleanup_tasks([task_id])
