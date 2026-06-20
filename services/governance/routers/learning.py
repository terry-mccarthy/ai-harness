"""Episode + candidate + skill-list endpoints: the learning pipeline."""
import json
import uuid
from datetime import datetime, timedelta

import pymysql
import pymysql.cursors
from fastapi import APIRouter, Header, HTTPException, Request

from core.auth import decode_jwt
from core.config import MIN_EPISODES, RECENT_DAYS
from core.dolt import get_dolt_conn, serialise_row
from core.opa import check_opa


router = APIRouter()


# ---------------------------------------------------------------------------
# List endpoints (read-only, any valid JWT)
# ---------------------------------------------------------------------------


@router.get("/episodes")
async def list_episodes(
    limit: int = 20,
    unlabeled: bool = False,
    authorization: str | None = Header(default=None),
):
    decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            where = " WHERE outcome_labeled_at IS NULL" if unlabeled else ""
            cur.execute(f"SELECT * FROM episodes{where} ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [serialise_row(r) for r in rows]


@router.get("/candidates")
async def list_candidates(
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if status:
                cur.execute(
                    "SELECT * FROM candidates WHERE status=%s ORDER BY created_at DESC",
                    (status.upper(),),
                )
            else:
                cur.execute("SELECT * FROM candidates ORDER BY created_at DESC")
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [serialise_row(r) for r in rows]


@router.get("/skills")
async def list_skills(
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if status:
                cur.execute(
                    """
                    SELECT s.* FROM skills s
                    INNER JOIN (
                        SELECT id, MAX(version) as max_v FROM skills WHERE status=%s GROUP BY id
                    ) t ON s.id = t.id AND s.version = t.max_v
                    """,
                    (status,),
                )
            else:
                cur.execute(
                    """
                    SELECT s.* FROM skills s
                    INNER JOIN (SELECT id, MAX(version) as max_v FROM skills GROUP BY id) t
                    ON s.id = t.id AND s.version = t.max_v
                    """
                )
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [serialise_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Episode labeling
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = {"RESOLVED", "FAILED", "ROLLED_BACK", "HUMAN_OVERRIDE", "INCONCLUSIVE"}


def _validate_label_body(outcome: str | None, outcome_signal: dict | None) -> None:
    if outcome not in _VALID_OUTCOMES:
        raise HTTPException(422, f"outcome must be one of {sorted(_VALID_OUTCOMES)}")
    if not outcome_signal:
        raise HTTPException(422, "outcome_signal must be non-empty")


def _check_episode_labelable(conn, episode_id: str, labeler_principal: str) -> dict:
    """Fetch episode and raise if it cannot be labeled. Returns the episode row."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT agent_principal, outcome_labeled_at FROM episodes WHERE episode_id=%s",
            (episode_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "episode_not_found")
    if row["outcome_labeled_at"] is not None:
        raise HTTPException(409, "episode_already_labeled")
    if row["agent_principal"] == labeler_principal:
        raise HTTPException(409, "self_label_not_permitted")
    return row


@router.post("/episodes/{episode_id}/label")
async def label_episode(
    episode_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = decode_jwt(authorization)
    if await check_opa("harness/label_allowed", {"scope": "episode:label", "agent_role": claims["role"]}) is not True:
        raise HTTPException(403, "episode_label_not_permitted")

    body = await request.json()
    outcome = body.get("outcome")
    outcome_signal = body.get("outcome_signal")
    labeler_principal = body.get("labeler_principal", claims["sub"])
    _validate_label_body(outcome, outcome_signal)

    conn = get_dolt_conn()
    try:
        _check_episode_labelable(conn, episode_id, labeler_principal)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE episodes SET outcome=%s, outcome_signal=%s, outcome_labeled_at=NOW(), human_actor=%s "
                "WHERE episode_id=%s",
                (outcome, json.dumps(outcome_signal), labeler_principal, episode_id),
            )
            cur.execute("CALL DOLT_COMMIT('-Am', %s)", (f"episode: {episode_id[:8]} labeled {outcome}",))
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM episodes WHERE episode_id=%s", (episode_id,))
            return serialise_row(cur.fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Candidate proposal
# ---------------------------------------------------------------------------

_K_MIN = 2       # minimum distinct agent_principals
_M_MIN = 2       # minimum recent episodes


def _fetch_and_qualify_episodes(conn, episode_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Return (qualified_rows, disqualified_ids). qualified_rows have outcome=RESOLVED + labeled."""
    fmt = ",".join(["%s"] * len(episode_ids))
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            f"SELECT episode_id, agent_principal, outcome, outcome_labeled_at "
            f"FROM episodes WHERE episode_id IN ({fmt})",
            episode_ids,
        )
        found = {r["episode_id"]: r for r in cur.fetchall()}

    qualified, disqualified = [], []
    for eid in episode_ids:
        row = found.get(eid)
        if row and row["outcome"] == "RESOLVED" and row["outcome_labeled_at"] is not None:
            qualified.append(row)
        else:
            disqualified.append(eid)
    return qualified, disqualified


def _compute_support_stats(qualified: list[dict]) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=RECENT_DAYS)
    return {
        "n_episodes": len(qualified),
        "n_principals": len({r["agent_principal"] for r in qualified}),
        "recent_count": sum(1 for r in qualified if r["outcome_labeled_at"] and r["outcome_labeled_at"] > cutoff),
    }


def _check_count_criteria(n_total: int, disqualified: list[str]) -> list[str]:
    errors = []
    if disqualified:
        errors.append(f"episodes not RESOLVED+labeled: {disqualified}")
    if n_total < MIN_EPISODES:
        errors.append(f"need at least {MIN_EPISODES} episodes, got {n_total}")
    return errors


def _count_recent_episodes(qualified: list[dict], cutoff: datetime) -> int:
    count = 0
    for r in qualified:
        if r.get("outcome_labeled_at") and r["outcome_labeled_at"] > cutoff:
            count += 1
    return count


def _check_diversity_criteria(qualified: list[dict]) -> list[str]:
    errors = []
    principals = {r["agent_principal"] for r in qualified}
    if len(principals) < _K_MIN:
        errors.append(f"need at least {_K_MIN} distinct agent_principals, got {len(principals)}")
    cutoff = datetime.utcnow() - timedelta(days=RECENT_DAYS)
    recent = _count_recent_episodes(qualified, cutoff)
    if recent < _M_MIN:
        errors.append(f"need at least {_M_MIN} episodes within last {RECENT_DAYS} days, got {recent}")
    return errors


def _check_candidate_criteria(
    qualified: list[dict],
    disqualified: list[str],
    n_total: int,
) -> list[str]:
    count_errors = _check_count_criteria(n_total, disqualified)
    if count_errors:
        return count_errors
    return _check_diversity_criteria(qualified)


@router.post("/candidates", status_code=201)
async def post_candidates(
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = decode_jwt(authorization)
    if await check_opa("harness/propose_allowed", {"scope": "candidate:propose", "agent_role": claims["role"]}) is not True:
        raise HTTPException(403, "candidate_propose_not_permitted")

    body = await request.json()
    episode_ids = body.get("episode_ids", [])
    cluster_key = body.get("cluster_key", "")
    proposed_procedure = body.get("proposed_procedure", {})

    conn = get_dolt_conn()
    try:
        qualified, disqualified = _fetch_and_qualify_episodes(conn, episode_ids)
        errors = _check_candidate_criteria(qualified, disqualified, len(episode_ids))
        if errors:
            raise HTTPException(422, {"errors": errors})

        support_stats = _compute_support_stats(qualified)

        candidate_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO candidates "
                "(candidate_id, cluster_key, member_episode_ids, proposed_procedure, support_stats, status) "
                "VALUES (%s,%s,%s,%s,%s,'PROPOSED')",
                (
                    candidate_id,
                    cluster_key,
                    json.dumps(episode_ids),
                    json.dumps(proposed_procedure),
                    json.dumps(support_stats),
                ),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"candidate: {candidate_id[:8]} proposed [{cluster_key}]",),
            )
    finally:
        conn.close()

    return {"candidate_id": candidate_id, "status": "PROPOSED"}


@router.get("/candidates/{candidate_id}")
async def get_candidate(
    candidate_id: str,
    authorization: str | None = Header(default=None),
):
    decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM candidates WHERE candidate_id=%s", (candidate_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(404, "candidate_not_found")
    return serialise_row(row)


# ---------------------------------------------------------------------------
# HITL promotion gate
# ---------------------------------------------------------------------------

_SKILL_EXPIRY_DAYS = 90


def _fetch_candidate_or_404(conn, candidate_id: str) -> dict:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM candidates WHERE candidate_id=%s", (candidate_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "candidate_not_found")
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray)):
            row[k] = v.decode()
    return row


def _fetch_latest_skill(conn, skill_id: str) -> dict | None:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM skills WHERE id=%s ORDER BY version DESC LIMIT 1",
            (skill_id,),
        )
        return cur.fetchone()


def _compute_procedure_diff(old_proc, new_proc) -> dict | None:
    if old_proc is None:
        return None
    old = old_proc if isinstance(old_proc, dict) else json.loads(old_proc)
    new = new_proc if isinstance(new_proc, dict) else json.loads(new_proc)
    if old == new:
        return None
    return {"before": old, "after": new}


def _insert_skill(conn, skill_id: str, candidate: dict, version: int, human_principal: str) -> None:
    procedure = candidate["proposed_procedure"]
    if isinstance(procedure, (bytes, bytearray)):
        procedure = procedure.decode()
    expires_at = datetime.utcnow() + timedelta(days=_SKILL_EXPIRY_DAYS)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skills "
            "(id, name, agent_role, version, status, description, input_schema, steps, "
            "output_contract, promoted_by, source_candidate_id, expires_at, created_at) "
            "VALUES (%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s,%s,NOW())",
            (
                skill_id,
                skill_id,
                skill_id.split(".")[0] if "." in skill_id else "unknown",
                version,
                f"Promoted from candidate {candidate['candidate_id'][:8]}",
                "{}",
                procedure if isinstance(procedure, str) else json.dumps(procedure),
                "{}",
                human_principal,
                candidate["candidate_id"],
                expires_at,
            ),
        )


@router.post("/candidates/{candidate_id}/promote")
async def promote_candidate(
    candidate_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = decode_jwt(authorization)
    if await check_opa("harness/promote_allowed", {"scope": "skill:promote", "agent_role": claims["role"]}) is not True:
        raise HTTPException(403, "skill_promote_not_permitted")

    conn = get_dolt_conn()
    try:
        candidate = _fetch_candidate_or_404(conn, candidate_id)
        if candidate["status"] == "PROMOTED":
            raise HTTPException(409, "candidate_already_promoted")

        episode_ids = candidate["member_episode_ids"]
        if isinstance(episode_ids, str):
            episode_ids = json.loads(episode_ids)
        qualified, disqualified = _fetch_and_qualify_episodes(conn, episode_ids)
        errors = _check_candidate_criteria(qualified, disqualified, len(episode_ids))
        if errors:
            raise HTTPException(422, {"errors": errors})

        cluster_key = candidate["cluster_key"]
        prior = _fetch_latest_skill(conn, cluster_key)
        new_version = (prior["version"] + 1) if prior else 1
        prior_proc = prior["steps"] if prior else None
        proc_diff = _compute_procedure_diff(prior_proc, candidate["proposed_procedure"])

        _insert_skill(conn, cluster_key, candidate, new_version, claims["sub"])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE candidates SET status='PROMOTED' WHERE candidate_id=%s",
                (candidate_id,),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"skill: promoted from candidate {candidate_id[:8]} by {claims['sub']}",),
            )
    finally:
        conn.close()

    return {"skill_id": cluster_key, "version": new_version, "procedure_diff": proc_diff}


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = decode_jwt(authorization)
    if await check_opa("harness/promote_allowed", {"scope": "skill:promote", "agent_role": claims["role"]}) is not True:
        raise HTTPException(403, "skill_promote_not_permitted")

    body = await request.json()
    reason = body.get("reason")
    if not reason:
        raise HTTPException(422, "reason is required")

    conn = get_dolt_conn()
    try:
        candidate = _fetch_candidate_or_404(conn, candidate_id)
        if candidate["status"] == "REJECTED":
            raise HTTPException(409, "candidate_already_rejected")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE candidates SET status='REJECTED' WHERE candidate_id=%s",
                (candidate_id,),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"candidate: {candidate_id[:8]} rejected by {claims['sub']}: {reason}",),
            )
    finally:
        conn.close()

    return {"status": "REJECTED", "reason": reason}
