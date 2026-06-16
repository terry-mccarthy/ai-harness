import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import time
import uuid

import httpx
import jwt
import pymysql
import pymysql.cursors
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Request

from core.config import (
    CLIENTS,
    DOLT_DB,
    DOLT_HOST,
    DOLT_PASSWORD,
    DOLT_PORT,
    DOLT_USER,
    EXPIRY_PASS_INTERVAL,
    MIN_EPISODES,
    OPA_URL,
    PRIVATE_KEY as _private_key,
    PUBLIC_KEY as _public_key,
    RECENT_DAYS,
    TOKEN_TTL,
    b64url as _b64url,
)
from core.auth import decode_jwt as _decode_jwt
from core.opa import check_opa
from core.dolt import (
    get_dolt_conn,
    serialise_row as _serialise_row,
    write_audit as _write_audit,
    write_episode as _write_episode,
)
from core.metrics import (
    tool_call_latency as _tool_call_latency,
    tool_calls_total as _tool_calls_total,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

app = FastAPI()

from routers.policy import router as policy_router
from routers.agents import router as agents_router
from routers.tasks import router as tasks_router

app.include_router(policy_router)
app.include_router(agents_router)
app.include_router(tasks_router)


# ---------------------------------------------------------------------------
# List endpoints (read-only, any valid JWT)
# ---------------------------------------------------------------------------


@app.get("/episodes")
async def list_episodes(
    limit: int = 20,
    unlabeled: bool = False,
    authorization: str | None = Header(default=None),
):
    _decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            where = " WHERE outcome_labeled_at IS NULL" if unlabeled else ""
            cur.execute(f"SELECT * FROM episodes{where} ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [_serialise_row(r) for r in rows]


@app.get("/candidates")
async def list_candidates(
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    _decode_jwt(authorization)
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
    return [_serialise_row(r) for r in rows]


@app.get("/skills")
async def list_skills(
    status: str | None = None,
    authorization: str | None = Header(default=None),
):
    _decode_jwt(authorization)
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
    return [_serialise_row(r) for r in rows]


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


@app.post("/episodes/{episode_id}/label")
async def label_episode(
    episode_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
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
            return _serialise_row(cur.fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Candidate proposal
# ---------------------------------------------------------------------------

_N_MIN = MIN_EPISODES
_K_MIN = 2       # minimum distinct agent_principals
_M_MIN = 2       # minimum recent episodes
_RECENT_DAYS = RECENT_DAYS


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
    cutoff = datetime.utcnow() - timedelta(days=_RECENT_DAYS)
    return {
        "n_episodes": len(qualified),
        "n_principals": len({r["agent_principal"] for r in qualified}),
        "recent_count": sum(1 for r in qualified if r["outcome_labeled_at"] and r["outcome_labeled_at"] > cutoff),
    }


def _check_count_criteria(n_total: int, disqualified: list[str]) -> list[str]:
    errors = []
    if disqualified:
        errors.append(f"episodes not RESOLVED+labeled: {disqualified}")
    if n_total < _N_MIN:
        errors.append(f"need at least {_N_MIN} episodes, got {n_total}")
    return errors


def _check_diversity_criteria(qualified: list[dict]) -> list[str]:
    errors = []
    principals = {r["agent_principal"] for r in qualified}
    if len(principals) < _K_MIN:
        errors.append(f"need at least {_K_MIN} distinct agent_principals, got {len(principals)}")
    cutoff = datetime.utcnow() - timedelta(days=_RECENT_DAYS)
    recent = sum(1 for r in qualified if r["outcome_labeled_at"] and r["outcome_labeled_at"] > cutoff)
    if recent < _M_MIN:
        errors.append(f"need at least {_M_MIN} episodes within last {_RECENT_DAYS} days, got {recent}")
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


@app.post("/candidates", status_code=201)
async def post_candidates(
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
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

        candidate_id = str(__import__("uuid").uuid4())
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


@app.get("/candidates/{candidate_id}")
async def get_candidate(
    candidate_id: str,
    authorization: str | None = Header(default=None),
):
    _decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM candidates WHERE candidate_id=%s", (candidate_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(404, "candidate_not_found")
    return _serialise_row(row)


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


@app.post("/candidates/{candidate_id}/promote")
async def promote_candidate(
    candidate_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
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


@app.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
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


# ---------------------------------------------------------------------------
# Skill read + revocation
# ---------------------------------------------------------------------------


@app.get("/skills/{skill_id}")
async def get_skill(
    skill_id: str,
    authorization: str | None = Header(default=None),
):
    _decode_jwt(authorization)
    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM skills WHERE id=%s ORDER BY version DESC LIMIT 1",
                (skill_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(404, "skill_not_found")
    if row["status"] in ("revoked", "expired"):
        raise HTTPException(410, f"skill_{row['status']}")
    return _serialise_row(row)


@app.post("/skills/{skill_id}/revoke")
async def revoke_skill(
    skill_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
    if await check_opa("harness/promote_allowed", {"scope": "skill:promote", "agent_role": claims["role"]}) is not True:
        raise HTTPException(403, "skill_promote_not_permitted")

    body = await request.json()
    reason = body.get("reason")
    if not reason:
        raise HTTPException(422, "reason is required")

    conn = get_dolt_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id FROM skills WHERE id=%s LIMIT 1", (skill_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, "skill_not_found")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skills SET status='revoked', revoked_reason=%s WHERE id=%s",
                (reason, skill_id),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"skill: {skill_id} revoked by {claims['sub']}: {reason}",),
            )
    finally:
        conn.close()

    return {"skill_id": skill_id, "status": "revoked", "reason": reason}


# ---------------------------------------------------------------------------
# Skill expiry and re-validation — issue 07
# ---------------------------------------------------------------------------


def _find_expired_skills(conn) -> list[dict]:
    """Return latest-version rows for ACTIVE skills whose expires_at has passed."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            """
            SELECT s.* FROM skills s
            INNER JOIN (
                SELECT id, MAX(version) as max_v
                FROM skills WHERE status='active' AND expires_at <= NOW()
                GROUP BY id
            ) t ON s.id = t.id AND s.version = t.max_v
            """
        )
        return cur.fetchall() or []


def _expire_skill(conn, skill_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE skills SET status='expired' WHERE id=%s", (skill_id,))
        cur.execute("CALL DOLT_COMMIT('-Am', %s)", (f"skill: {skill_id} expired",))


def _find_active_skills(conn) -> list[dict]:
    """Return latest-version rows for ACTIVE skills (not yet expired)."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            """
            SELECT s.* FROM skills s
            INNER JOIN (
                SELECT id, MAX(version) as max_v
                FROM skills WHERE status='active'
                GROUP BY id
            ) t ON s.id = t.id AND s.version = t.max_v
            """
        )
        return cur.fetchall() or []


def _find_revalidation_episodes(conn, agent_role: str) -> list[dict]:
    """Return recent RESOLVED episodes written by agents with the given role."""
    cutoff = datetime.utcnow() - timedelta(days=_RECENT_DAYS)
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM episodes WHERE agent_principal=%s AND outcome='RESOLVED' AND created_at >= %s",
            (agent_role, cutoff),
        )
        return cur.fetchall() or []


def _maybe_repropose_candidate(conn, skill: dict) -> str | None:
    """Auto-propose a candidate if enough recent resolved episodes exist. Returns candidate_id or None."""
    episodes = _find_revalidation_episodes(conn, skill["agent_role"])
    if len(episodes) < _N_MIN:
        return None
    candidate_id = str(uuid.uuid4())
    cluster_key = skill["id"]
    episode_ids = [ep["episode_id"] for ep in episodes[:_N_MIN]]
    stats = {"n_episodes": len(episode_ids), "auto_revalidation": True}
    procedure = skill["steps"]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidates "
            "(candidate_id, cluster_key, member_episode_ids, proposed_procedure, support_stats, status) "
            "VALUES (%s, %s, %s, %s, %s, 'PROPOSED')",
            (candidate_id, cluster_key, json.dumps(episode_ids), procedure, json.dumps(stats)),
        )
        cur.execute(
            "CALL DOLT_COMMIT('-Am', %s)",
            (f"candidate: {candidate_id[:8]} auto-proposed [{cluster_key}]",),
        )
    return candidate_id


def _compute_early_review_flags(conn, active_skills: list[dict]) -> list[str]:
    """Return skill IDs whose trailing 30-day audit success rate is < 0.5."""
    flagged = []
    cutoff_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    for skill in active_skills:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT COUNT(*) as total, SUM(policy_decision='allow') as allowed "
                "FROM audit_log WHERE agent_id=%s AND timestamp_ms >= %s",
                (skill["agent_role"], cutoff_ms),
            )
            row = cur.fetchone()
        total = int(row["total"] or 0) if row else 0
        if total == 0:
            continue
        allowed = int(row["allowed"] or 0)
        if (allowed / total) < 0.5:
            flagged.append(skill["id"])
    return flagged


def _run_expiry_pass(conn) -> dict:
    """Expire overdue skills, auto-propose re-validation candidates, flag low-success skills."""
    expired_skills = _find_expired_skills(conn)
    skill_ids = []
    re_proposed = []
    for skill in expired_skills:
        _expire_skill(conn, skill["id"])
        skill_ids.append(skill["id"])
        candidate_id = _maybe_repropose_candidate(conn, skill)
        if candidate_id:
            re_proposed.append(candidate_id)
    active_skills = _find_active_skills(conn)
    flagged = _compute_early_review_flags(conn, active_skills)
    return {
        "expired_count": len(expired_skills),
        "skill_ids": skill_ids,
        "re_proposed_candidates": re_proposed,
        "flagged_for_early_review": flagged,
    }


def _background_expiry_pass() -> None:
    conn = None
    try:
        conn = get_dolt_conn()
        _run_expiry_pass(conn)
    except Exception as e:
        logger.warning("background expiry pass failed: %s", e)
    finally:
        if conn:
            conn.close()


@app.post("/skills/expire")
async def expire_skills(
    authorization: str | None = Header(default=None),
):
    """Expire overdue skills and trigger re-validation candidate proposal."""
    claims = _decode_jwt(authorization)
    if await check_opa("harness/promote_allowed", {"scope": "skill:promote", "agent_role": claims["role"]}) is not True:
        raise HTTPException(403, "skill_promote_not_permitted")
    conn = get_dolt_conn()
    try:
        result = _run_expiry_pass(conn)
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Skill selection — issue 08
# ---------------------------------------------------------------------------


def _fetch_active_skills_for_select(conn) -> list[dict]:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            """
            SELECT s.* FROM skills s
            INNER JOIN (
                SELECT id, MAX(version) as max_v
                FROM skills WHERE status='active'
                GROUP BY id
            ) t ON s.id = t.id AND s.version = t.max_v
            """
        )
        return cur.fetchall() or []


def _parse_preconditions(raw) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _specificity_score(skill: dict, env_fingerprint: dict) -> int:
    raw = skill.get("preconditions")
    if not raw:
        return 0
    prec = _parse_preconditions(raw)
    return sum(1 for k, v in prec.get("env_constraints", {}).items() if env_fingerprint.get(k) == v)


def _skill_success_rate(conn, skill: dict) -> float:
    cutoff_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT COUNT(*) as total, SUM(policy_decision='allow') as allowed "
            "FROM audit_log WHERE agent_id=%s AND timestamp_ms >= %s",
            (skill["agent_role"], cutoff_ms),
        )
        row = cur.fetchone()
    if not row or not row["total"]:
        return 0.0
    return float(row["allowed"] or 0) / float(row["total"])


def _apply_specificity_rule(skills: list, env_fingerprint: dict) -> list:
    scored = [(s, _specificity_score(s, env_fingerprint)) for s in skills]
    best = max(sc for _, sc in scored)
    return [(s, sc) for s, sc in scored if sc == best]


def _apply_recency_rule(candidates: list) -> tuple:
    best_ts = max(s["created_at"] for s, _ in candidates)
    survivors = [(s, sc) for s, sc in candidates if s["created_at"] == best_ts]
    ts_val = best_ts.isoformat() if hasattr(best_ts, "isoformat") else str(best_ts)
    return survivors, ts_val


def _apply_success_rate_rule(conn, candidates: list) -> list:
    rated = [(s, sc, _skill_success_rate(conn, s)) for s, sc in candidates]
    best = max(r for _, _, r in rated)
    return [(s, sc, r) for s, sc, r in rated if r == best]


def _run_skill_selection(conn, env_fingerprint: dict) -> dict:
    skills = _fetch_active_skills_for_select(conn)
    if not skills:
        return {"winner": None, "tied": [], "reason": "no active skills"}

    candidates = _apply_specificity_rule(skills, env_fingerprint)
    if len(candidates) == 1:
        s, sc = candidates[0]
        return {"winner": s, "rule": "specificity", "score": sc}

    candidates, ts_val = _apply_recency_rule(candidates)
    if len(candidates) == 1:
        return {"winner": candidates[0][0], "rule": "recency", "score": ts_val}

    rated = _apply_success_rate_rule(conn, candidates)
    if len(rated) == 1:
        s, sc, r = rated[0]
        return {"winner": s, "rule": "success_rate", "score": r}

    tied = [{"id": s["id"], "specificity": sc, "success_rate": r} for s, sc, r in rated]
    return {"winner": None, "tied": tied, "reason": f"tied: {[s['id'] for s, sc, r in rated]}"}


@app.post("/skills/select")
async def select_skill(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    """Select the best ACTIVE skill using ordered specificity → recency → success-rate tiebreaks."""
    claims = _decode_jwt(authorization)
    body = await request.json()
    env_fingerprint = body.get("env_fingerprint") or {}

    conn = get_dolt_conn()
    try:
        result = _run_skill_selection(conn, env_fingerprint)
    finally:
        conn.close()

    winner = result.get("winner")
    policy_rule = f"selected[{result.get('rule')}]:{winner['id']}" if winner else "escalated"
    background_tasks.add_task(
        _write_audit,
        claims["sub"], "skill:select", "skill:select", "", "", "allow", policy_rule, 0, None,
    )

    if winner:
        return {"selected": winner["id"], "rationale": {"rule": result["rule"], "score": result["score"]}}
    return {
        "selected": None,
        "escalate": True,
        "reason": result.get("reason", "no winner"),
        "tied_skills": result.get("tied", []),
    }


