import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
import time
import uuid

import httpx
import jwt
import pymysql
import pymysql.cursors
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

app = FastAPI()

# ---------------------------------------------------------------------------
# RSA keypair — load from file (preferred) or inline PEM env var
# ---------------------------------------------------------------------------

_TEST_KEY_FINGERPRINT = "sha256:f51572658f267e254a18caf6d2320581aacfbaee028a2a875a8a47af4630ffb5"

_key_file = os.environ.get("JWT_PRIVATE_KEY_FILE")
if _key_file:
    with open(_key_file, "rb") as _f:
        _jwt_private_key_pem = _f.read()
else:
    _jwt_private_key_pem = os.environ["JWT_PRIVATE_KEY"].encode()

_key_fingerprint = "sha256:" + hashlib.sha256(_jwt_private_key_pem).hexdigest()
if _key_fingerprint == _TEST_KEY_FINGERPRINT and os.environ.get("ENV") != "test":
    raise RuntimeError(
        "Governance is configured with the committed test key. "
        "Set ENV=test or supply a production key via JWT_PRIVATE_KEY_FILE."
    )

_private_key = load_pem_private_key(_jwt_private_key_pem, password=None)
_public_key = _private_key.public_key()


def _b64url(n: int) -> str:
    byte_len = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(byte_len, "big")).rstrip(b"=").decode()


OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
DOLT_HOST = os.environ.get("DOLT_HOST", "dolt")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
DOLT_USER = os.environ.get("DOLT_USER", "harness")
DOLT_PASSWORD = os.environ.get("DOLT_PASSWORD", "harness")
DOLT_DB = os.environ.get("DOLT_DB", "harness")
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "900"))  # 15 min
EXPIRY_PASS_INTERVAL = int(os.environ.get("EXPIRY_PASS_INTERVAL", "1000"))
_audit_call_count: int = 0

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
_tool_calls_total = Counter(
    "harness_tool_calls_total",
    "Total tool invocations by agent role and decision",
    ["agent_role", "decision"],
)
_tool_call_latency = Histogram(
    "harness_tool_call_latency_ms",
    "Tool call latency in milliseconds",
    ["agent_role"],
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)

CLIENTS = {
    "architect": {
        "secret": os.environ.get("ARCHITECT_SECRET", "architect-secret"),
        "role": "architect",
    },
    "code-reviewer": {
        "secret": os.environ["CODE_REVIEWER_SECRET"],
        "role": "code_reviewer",
    },
    "sre": {
        "secret": os.environ.get("SRE_SECRET", "sre-secret"),
        "role": "sre",
    },
    "human-operator": {
        "secret": os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"),
        "role": "human_operator",
    },
}


def _decode_jwt(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing_token")
    raw_token = authorization[7:]
    try:
        return jwt.decode(raw_token, _public_key, algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid_token")


async def _check_opa(role: str, short_tool: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            opa_resp = await client.post(
                f"{OPA_URL}/v1/data/harness/allow",
                json={"input": {"agent_role": role, "tool_name": short_tool}},
                timeout=5.0,
            )
        return opa_resp.json().get("result", False)
    except Exception as e:
        logger.error("OPA unreachable: %s", e)
        return False


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=DOLT_USER,
        password=DOLT_PASSWORD,
        database=DOLT_DB,
        autocommit=True,
    )


def _write_episode(
    agent_principal, tool_name, short_tool, req_hash, correlation_id, service_class,
):
    conn = None
    try:
        import uuid as _uuid
        episode_id = str(_uuid.uuid4())
        timestamp_ms = int(time.time() * 1000)
        alert_sig = f"{agent_principal}.{short_tool}:{correlation_id or ''}"
        env_fp = json.dumps({"tool_name": tool_name, "server_id": short_tool, "timestamp_ms": timestamp_ms})
        actions = json.dumps([{"tool": tool_name, "scoped_args": req_hash, "scope_token_ref": correlation_id}])
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes "
                "(episode_id, agent_principal, alert_signature, service_class, env_fingerprint, actions) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (episode_id, agent_principal, alert_sig, service_class or "unknown", env_fp, actions),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"episode: {short_tool} by {agent_principal}",),
            )
    except Exception as e:
        logger.error("Dolt episode write failed: %s", e)
    finally:
        if conn:
            conn.close()


def _write_audit(
    agent_id, tool_name, server_id, req_hash, resp_hash,
    decision, rule, latency_ms, correlation_id=None,
):
    conn = None
    try:
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_log
                   (agent_id, tool_name, server_id, request_hash, response_hash,
                    policy_decision, policy_rule, timestamp_ms, latency_ms, correlation_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    agent_id,
                    tool_name,
                    server_id,
                    req_hash,
                    resp_hash,
                    decision,
                    rule,
                    int(time.time() * 1000),
                    latency_ms,
                    correlation_id,
                ),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"audit: {tool_name} by {agent_id} [{decision}]",),
            )
    except Exception as e:
        logger.error("Dolt audit write failed: %s", e)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    client = CLIENTS.get(client_id)
    if not client or client["secret"] != client_secret:
        raise HTTPException(401, "invalid_client")

    now = int(time.time())
    payload = {
        "sub": client_id,
        "role": client["role"],
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    access_token = jwt.encode(payload, _private_key, algorithm="RS256")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL,
    }


# ---------------------------------------------------------------------------
# Policy check — replaces the old proxy endpoint
# ---------------------------------------------------------------------------


@app.post("/check")
async def check_policy(
    request: Request,
    authorization: str | None = Header(default=None),
    x_human_approval_token: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Validate token and OPA policy. Returns {allowed, role, agent_id, rule}."""
    claims = _decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("tool_name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = f"harness.allow[{claims['role']}]"

    if short_tool == "shell_exec" and not x_human_approval_token:
        _tool_calls_total.labels(agent_role=claims["role"], decision="deny").inc()
        _write_audit(
            claims["sub"], full_tool, short_tool, "", "", "deny",
            "shell_exec_requires_human_approval", 0, x_correlation_id,
        )
        raise HTTPException(403, "shell_exec_requires_human_approval")

    if not await _check_opa(claims["role"], short_tool):
        _tool_calls_total.labels(agent_role=claims["role"], decision="deny").inc()
        _write_audit(
            claims["sub"], full_tool, short_tool, "", "", "deny",
            rule, 0, x_correlation_id,
        )
        raise HTTPException(403, "policy_denied")

    return {
        "allowed": True,
        "role": claims["role"],
        "agent_id": claims["sub"],
        "rule": rule,
    }


# ---------------------------------------------------------------------------
# Audit — async Dolt write called by GatewayClient post-invocation
# ---------------------------------------------------------------------------


@app.post("/audit", status_code=202)
async def audit(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Accept an audit record from GatewayClient and write it to Dolt async."""
    global _audit_call_count
    claims = _decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("tool_name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = body.get("rule", f"harness.allow[{claims['role']}]")
    req_hash = body.get("req_hash", "")
    resp_hash = body.get("resp_hash", "")
    decision = body.get("decision", "allow")
    latency_ms = int(body.get("latency_ms", 0))
    correlation_id = x_correlation_id or body.get("correlation_id")

    _tool_calls_total.labels(agent_role=claims["role"], decision=decision).inc()
    if latency_ms:
        _tool_call_latency.labels(agent_role=claims["role"]).observe(latency_ms)

    background_tasks.add_task(
        _write_audit,
        claims["sub"],
        full_tool,
        short_tool,
        req_hash,
        resp_hash,
        decision,
        rule,
        latency_ms,
        correlation_id,
    )
    background_tasks.add_task(
        _write_episode,
        claims["sub"],
        full_tool,
        short_tool,
        req_hash,
        correlation_id,
        body.get("service_class"),
    )
    _audit_call_count += 1
    if EXPIRY_PASS_INTERVAL > 0 and _audit_call_count % EXPIRY_PASS_INTERVAL == 0:
        background_tasks.add_task(_background_expiry_pass)
    return {}


# ---------------------------------------------------------------------------
# JWKS — public key for downstream verifiers
# ---------------------------------------------------------------------------


@app.get("/jwks")
async def jwks():
    pub = _public_key.public_numbers()
    return {
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "1",
            "n": _b64url(pub.n),
            "e": _b64url(pub.e),
        }]
    }


# ---------------------------------------------------------------------------
# Episode labeling
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = {"RESOLVED", "FAILED", "ROLLED_BACK", "HUMAN_OVERRIDE", "INCONCLUSIVE"}


def _serialise_row(row: dict) -> dict:
    """Convert datetime and bytes values in a Dolt row to JSON-safe types."""
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else v.decode() if isinstance(v, (bytes, bytearray)) else v)
        for k, v in row.items()
    }


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


async def _check_opa_label(role: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OPA_URL}/v1/data/harness/label_allowed",
                json={"input": {"scope": "episode:label", "agent_role": role}},
                timeout=5.0,
            )
        return resp.json().get("result", False) is True
    except Exception as e:
        logger.error("OPA label check failed: %s", e)
        return False


@app.post("/episodes/{episode_id}/label")
async def label_episode(
    episode_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
    if not await _check_opa_label(claims["role"]):
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

_N_MIN = 5       # minimum episode count
_K_MIN = 2       # minimum distinct agent_principals
_M_MIN = 2       # minimum recent episodes
_RECENT_DAYS = 90


async def _check_opa_propose(role: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OPA_URL}/v1/data/harness/propose_allowed",
                json={"input": {"scope": "candidate:propose", "agent_role": role}},
                timeout=5.0,
            )
        return resp.json().get("result", False) is True
    except Exception as e:
        logger.error("OPA propose check failed: %s", e)
        return False


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
    if not await _check_opa_propose(claims["role"]):
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


async def _check_opa_promote(role: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OPA_URL}/v1/data/harness/promote_allowed",
                json={"input": {"scope": "skill:promote", "agent_role": role}},
                timeout=5.0,
            )
        return resp.json().get("result", False) is True
    except Exception as e:
        logger.error("OPA promote check failed: %s", e)
        return False


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
    if not await _check_opa_promote(claims["role"]):
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
    if not await _check_opa_promote(claims["role"]):
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
    if not await _check_opa_promote(claims["role"]):
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
    if not await _check_opa_promote(claims["role"]):
        raise HTTPException(403, "skill_promote_not_permitted")
    conn = get_dolt_conn()
    try:
        result = _run_expiry_pass(conn)
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Metrics + security endpoints (unchanged)
# ---------------------------------------------------------------------------


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Agent registry and discovery
# ---------------------------------------------------------------------------

MCPJUNGLE_URL = os.environ.get("MCPJUNGLE_URL", "http://mcpjungle:8080")

# Agent registry: known agents with their credentials, entry tools, and input schemas
_AGENT_REGISTRY: dict[str, dict] = {
    "code-reviewer": {
        "client_id": "code-reviewer",
        "secret_env": "CODE_REVIEWER_SECRET",
        "role": "code_reviewer",
        "entry_tool": "review_server__review_diff",
        "input_schema": {
            "type": "object",
            "required": ["repo"],
            "properties": {
                "repo": {"type": "string"},
                "base_ref": {"type": "string"},
                "head_ref": {"type": "string"},
                "diff_text": {"type": "string"},
            },
        },
    },
    "architect": {
        "client_id": "architect",
        "secret_env": "ARCHITECT_SECRET",
        "role": "architect",
        "entry_tool": "architect_stub__codebase_search",
        "input_schema": {
            "type": "object",
            "required": [],
            "properties": {
                "query": {"type": "string"},
                "decision": {"type": "string"},
            },
        },
    },
    "sre": {
        "client_id": "sre",
        "secret_env": "SRE_SECRET",
        "role": "sre",
        "entry_tool": "sre_stub__observability_query",
        "input_schema": {
            "type": "object",
            "required": [],
            "properties": {
                "query": {"type": "string"},
                "alert": {"type": "string"},
            },
        },
    },
}

_KNOWN_AGENTS = list(_AGENT_REGISTRY.keys())


async def _check_opa_invoke(role: str, target: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OPA_URL}/v1/data/harness/invoke_allowed",
                json={"input": {"role": role, "action": "invoke", "target": target}},
                timeout=5.0,
            )
        result = resp.json().get("result", [])
        return target in result
    except Exception as e:
        logger.error("OPA invoke check failed: %s", e)
        return False


def _validate_payload(schema: dict, payload: dict) -> list[str]:
    """Return list of validation errors, or empty list if valid."""
    errors = []
    required = schema.get("required", [])
    for field in required:
        if field not in payload:
            errors.append(f"missing required field: {field}")
    return errors


async def _call_mcpjungle(tool_name: str, params: dict) -> dict:
    body = {"name": tool_name, **params}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MCPJUNGLE_URL}/api/v0/tools/invoke",
            json=body,
            timeout=60.0,
        )
    data = resp.json()
    # Unwrap MCPJungle content wrapper
    content = data.get("content", [])
    if content and isinstance(content, list) and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            return {"text": content[0].get("text", "")}
    return data


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


@app.post("/tasks/complete")
async def task_complete(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Idempotently close a claimed task with a result."""
    claims = _decode_jwt(authorization)
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


@app.post("/agent/invoke")
async def agent_invoke(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Synchronous governed handoff: validate OPA, mint target creds, forward."""
    claims = _decode_jwt(authorization)
    caller_role = claims["role"]
    body = await request.json()
    correlation_id = x_correlation_id
    target = body.get("target", "")
    artifact_type = body.get("artifact_type", "")
    payload = body.get("payload", {})

    # Unknown target
    if target not in _AGENT_REGISTRY:
        raise HTTPException(404, f"unknown target: {target}")

    agent_spec = _AGENT_REGISTRY[target]

    # Payload schema validation (before any OPA/network call)
    errors = _validate_payload(agent_spec["input_schema"], payload)
    if errors:
        raise HTTPException(422, {"errors": errors})

    # OPA invoke check
    invoke_ok = await _check_opa_invoke(caller_role, target)
    if not invoke_ok:
        # Denied — write audit row synchronously (HTTPException cancels background tasks)
        _write_audit(
            claims["sub"],
            f"agent_invoke:{target}",
            target,
            "",
            "",
            "deny",
            f"invoke_denied[{caller_role}->{target}]",
            0,
            correlation_id,
        )
        raise HTTPException(403, "invoke_denied_by_policy")

    # Mint target's own token (do NOT forward caller's token)
    secret = os.environ.get(agent_spec["secret_env"], f"{agent_spec['client_id']}-secret")
    now = int(time.time())
    target_token_payload = {
        "sub": agent_spec["client_id"],
        "role": agent_spec["role"],
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    target_token = jwt.encode(target_token_payload, _private_key, algorithm="RS256")

    # Call MCPJungle entry tool using target's identity
    result = await _call_mcpjungle(agent_spec["entry_tool"], payload)

    # Write audit as the target agent
    background_tasks.add_task(
        _write_audit,
        agent_spec["client_id"],
        f"agent_invoke:{agent_spec['entry_tool']}",
        agent_spec["entry_tool"],
        "",
        "",
        "allow",
        f"invoke_allowed[{caller_role}->{target}]",
        0,
        correlation_id,
    )

    return result


@app.get("/agents")
async def agent_list(authorization: str | None = Header(default=None)):
    """Return the list of agents the calling role is permitted to invoke."""
    claims = _decode_jwt(authorization)
    role = claims["role"]
    permitted = []
    for name in _KNOWN_AGENTS:
        if await _check_opa_invoke(role, name):
            permitted.append({"name": name})
    return permitted


# ---------------------------------------------------------------------------
# Blackboard: task_post + task_claim
# ---------------------------------------------------------------------------

@app.post("/tasks")
async def task_post(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Create a pending task on the blackboard."""
    _decode_jwt(authorization)
    body = await request.json()
    required_role = body.get("required_role")
    artifact_type = body.get("artifact_type")
    payload = body.get("payload", {})
    priority = int(body.get("priority", 0))

    if not required_role or not artifact_type:
        raise HTTPException(422, "required_role and artifact_type are required")

    task_id = str(__import__("uuid").uuid4())
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


@app.post("/tasks/claim")
async def task_claim(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Atomically claim the highest-priority pending task for the caller's role."""
    claims = _decode_jwt(authorization)
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


@app.post("/memory/write")
async def memory_write(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Auth-gated memory write proxy. Requires a valid Bearer token."""
    _decode_jwt(authorization)
    body = await request.json()
    logger.info(
        "memory_write: ns=%s key=%s by agent=%s",
        body.get("namespace"),
        body.get("key"),
        "authenticated",
    )
    return {"status": "ok"}
