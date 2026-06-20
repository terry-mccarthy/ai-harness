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


def _close_task(conn, task_id: str, result: dict, idem_key: str | None, worker: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='done', result=%s, idempotency_key=%s "
            "WHERE id=%s AND status='claimed' AND claimed_by=%s",
            (json.dumps(result), idem_key, task_id, worker),
        )
        return cur.rowcount == 1


def _commit_task_complete(conn, task_id: str, worker: str) -> None:
    with conn.cursor() as cur:
        cur.execute("CALL DOLT_COMMIT('-Am', %s)", (f"task_complete: {task_id[:8]} by {worker}",))


@router.post("/tasks/complete")
async def task_complete(
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = decode_jwt(authorization)
    body = await request.json()
    task_id = body.get("task_id")
    if not task_id:
        raise HTTPException(422, "task_id is required")

    conn = get_dolt_conn()
    try:
        idem_key = body.get("idempotency_key")
        if idem_key:
            cached = _resolve_idempotent_complete(conn, idem_key)
            if cached:
                return cached

        if not _close_task(conn, task_id, body.get("result", {}), idem_key, claims["sub"]):
            raise HTTPException(403, "task not claimable by this worker or already done")

        _commit_task_complete(conn, task_id, claims["sub"])
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


def _reap_stale_leases(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='pending', claimed_by=NULL, lease_expires=NULL "
            "WHERE status='claimed' AND lease_expires < %s",
            (datetime.utcnow(),),
        )


def _pick_pending(conn, role: str) -> str | None:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT id FROM tasks "
            "WHERE status='pending' AND required_role=%s "
            "ORDER BY priority DESC, created_at ASC LIMIT 1",
            (role,),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def _try_claim(conn, candidate_id: str, worker_id: str, lease_expires: datetime) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='claimed', claimed_by=%s, lease_expires=%s "
            "WHERE id=%s AND status='pending'",
            (worker_id, lease_expires, candidate_id),
        )
        return cur.rowcount == 1


def _fetch_task_payload(conn, task_id: str) -> dict:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT payload FROM tasks WHERE id=%s", (task_id,))
        row = cur.fetchone()
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


def _commit_claim(conn, task_id: str, worker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CALL DOLT_COMMIT('-Am', %s)",
            (f"task_claim: {task_id[:8]} by {worker_id}",),
        )


@router.post("/tasks/claim")
async def task_claim(
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = decode_jwt(authorization)
    role = claims["role"]
    body = await request.json()
    lease_seconds = int(body.get("lease_seconds", 120))
    lease_expires = datetime.utcnow() + timedelta(seconds=lease_seconds)
    worker_id = claims["sub"]

    conn = get_dolt_conn()
    try:
        _reap_stale_leases(conn)

        for _ in range(5):
            candidate_id = _pick_pending(conn, role)
            if candidate_id is None:
                return {"task_id": None}
            if _try_claim(conn, candidate_id, worker_id, lease_expires):
                payload = _fetch_task_payload(conn, candidate_id)
                _commit_claim(conn, candidate_id, worker_id)
                return {"task_id": candidate_id, "payload": payload}

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
