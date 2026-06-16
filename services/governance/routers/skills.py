"""Skill lifecycle endpoints: CRUD, revoke, expire, select."""
import json
import logging
import uuid
from datetime import datetime, timedelta

import pymysql
import pymysql.cursors
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from core.auth import decode_jwt
from core.config import MIN_EPISODES, RECENT_DAYS
from core.dolt import get_dolt_conn, serialise_row, write_audit
from core.opa import check_opa


logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Skill read + revocation
# ---------------------------------------------------------------------------


@router.get("/skills/{skill_id}")
async def get_skill(
    skill_id: str,
    authorization: str | None = Header(default=None),
):
    decode_jwt(authorization)
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
    return serialise_row(row)


@router.post("/skills/{skill_id}/revoke")
async def revoke_skill(
    skill_id: str,
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
    cutoff = datetime.utcnow() - timedelta(days=RECENT_DAYS)
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM episodes WHERE agent_principal=%s AND outcome='RESOLVED' AND created_at >= %s",
            (agent_role, cutoff),
        )
        return cur.fetchall() or []


def _maybe_repropose_candidate(conn, skill: dict) -> str | None:
    """Auto-propose a candidate if enough recent resolved episodes exist. Returns candidate_id or None."""
    episodes = _find_revalidation_episodes(conn, skill["agent_role"])
    if len(episodes) < MIN_EPISODES:
        return None
    candidate_id = str(uuid.uuid4())
    cluster_key = skill["id"]
    episode_ids = [ep["episode_id"] for ep in episodes[:MIN_EPISODES]]
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


def background_expiry_pass() -> None:
    """Public — lazy-imported by routers/policy.py's /audit handler."""
    conn = None
    try:
        conn = get_dolt_conn()
        _run_expiry_pass(conn)
    except Exception as e:
        logger.warning("background expiry pass failed: %s", e)
    finally:
        if conn:
            conn.close()


@router.post("/skills/expire")
async def expire_skills(
    authorization: str | None = Header(default=None),
):
    """Expire overdue skills and trigger re-validation candidate proposal."""
    claims = decode_jwt(authorization)
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


@router.post("/skills/select")
async def select_skill(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    """Select the best ACTIVE skill using ordered specificity → recency → success-rate tiebreaks."""
    claims = decode_jwt(authorization)
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
        write_audit,
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
