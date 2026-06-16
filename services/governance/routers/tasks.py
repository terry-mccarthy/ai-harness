"""Blackboard task queue and memory-proxy endpoints."""
from datetime import datetime, timedelta
import json
import logging
import uuid

import pymysql
import pymysql.cursors
from fastapi import APIRouter, HTTPException, Header, Request

from core.auth import decode_jwt
from core.dolt import get_dolt_conn

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_idempotent_complete(conn, idem_key: str) -> dict | None:
    """Return cached result dict if this idempotency_key was already completed, else None."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT result FROM tasks WHERE idempotency_key = %s AND status = 'done'",
            (idem_key,),
        )
        existing = cur.fetchone()
    if not existing:
        return None
    stored = existing["result"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    return {"status": "done", "result": stored}


@router.post("/tasks/complete")
async def task_complete(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Idempotently close a claimed task with a result."""
    claims = decode_jwt(authorization)
    body = await request.json()
    task_id = body.get("task_id")
    result = body.get("result", {})
    idem_key = body.get("idempotency_key")

    if not task_id:
        raise HTTPException(422, "task_id is required")

    conn = get_dolt_conn()
    try:
        if idem_key:
            cached = _resolve_idempotent_complete(conn, idem_key)
            if cached:
                return cached

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status='done', result=%s, idempotency_key=%s "
                "WHERE id=%s AND status='claimed' AND claimed_by=%s",
                (json.dumps(result), idem_key, task_id, claims["sub"]),
            )
            affected = cur.rowcount

        if affected == 0:
            raise HTTPException(403, "task not claimable by this worker or already done")

        with conn.cursor() as cur:
            cur.execute("CALL DOLT_COMMIT('-Am', %s)", (f"task_complete: {task_id[:8]} by {claims['sub']}",))
    finally:
        conn.close()

    return {"status": "done"}


@router.post("/tasks")
async def task_post(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Create a pending task on the blackboard."""
    decode_jwt(authorization)
    body = await request.json()
    required_role = body.get("required_role")
    artifact_type = body.get("artifact_type")
    payload = body.get("payload", {})
    priority = int(body.get("priority", 0))

    if not required_role or not artifact_type:
        raise HTTPException(422, "required_role and artifact_type are required")

    task_id = str(uuid.uuid4())
    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, required_role, artifact_type, payload, priority, status) "
                "VALUES (%s, %s, %s, %s, %s, 'pending')",
                (task_id, required_role, artifact_type, json.dumps(payload), priority),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"task_post: {artifact_type} for {required_role} [{task_id[:8]}]",),
            )
    finally:
        conn.close()

    return {"task_id": task_id, "status": "pending"}


@router.post("/tasks/claim")
async def task_claim(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Atomically claim the highest-priority pending task for the caller's role."""
    claims = decode_jwt(authorization)
    role = claims["role"]
    body = await request.json()
    lease_seconds = int(body.get("lease_seconds", 120))
    lease_expires = datetime.utcnow() + timedelta(seconds=lease_seconds)
    worker_id = claims["sub"]

    conn = get_dolt_conn()
    try:
        # Reap stale leases first (on-claim sweep)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status='pending', claimed_by=NULL, lease_expires=NULL "
                "WHERE status='claimed' AND lease_expires < %s",
                (datetime.utcnow(),),
            )

        # Atomic select-then-update loop
        for _ in range(5):
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT id FROM tasks "
                    "WHERE status='pending' AND required_role=%s "
                    "ORDER BY priority DESC, created_at ASC LIMIT 1",
                    (role,),
                )
                row = cur.fetchone()

            if row is None:
                return {"task_id": None}

            candidate_id = row["id"]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET status='claimed', claimed_by=%s, lease_expires=%s "
                    "WHERE id=%s AND status='pending'",
                    (worker_id, lease_expires, candidate_id),
                )
                affected = cur.rowcount

            if affected == 1:
                # Win — fetch payload and commit
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute("SELECT payload FROM tasks WHERE id=%s", (candidate_id,))
                    task_row = cur.fetchone()
                with conn.cursor() as cur:
                    cur.execute(
                        "CALL DOLT_COMMIT('-Am', %s)",
                        (f"task_claim: {candidate_id[:8]} by {worker_id}",),
                    )
                payload = json.loads(task_row["payload"]) if isinstance(task_row["payload"], str) else task_row["payload"]
                return {"task_id": candidate_id, "payload": payload}
            # else: lost the race — retry

        return {"task_id": None}
    finally:
        conn.close()


@router.post("/memory/write")
async def memory_write(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Auth-gated memory write proxy. Requires a valid Bearer token."""
    decode_jwt(authorization)
    body = await request.json()
    logger.info(
        "memory_write: ns=%s key=%s by agent=%s",
        body.get("namespace"),
        body.get("key"),
        "authenticated",
    )
    return {"status": "ok"}
