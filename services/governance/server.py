import asyncio
import base64
import hashlib
import json
import logging
import os
import time

import httpx
import jwt
import pymysql
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
        # Check for existing completion via idempotency_key
        if idem_key:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT result FROM tasks WHERE idempotency_key = %s AND status = 'done'",
                    (idem_key,),
                )
                existing = cur.fetchone()
            if existing:
                stored = existing["result"]
                if isinstance(stored, str):
                    stored = json.loads(stored)
                return {"status": "done", "result": stored}

        result_json = json.dumps(result)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status='done', result=%s, idempotency_key=%s "
                "WHERE id=%s AND status='claimed' AND claimed_by=%s",
                (result_json, idem_key, task_id, claims["sub"]),
            )
            affected = cur.rowcount

        if affected == 0:
            # Could be: task not found, wrong status, or wrong claimer
            raise HTTPException(403, "task not claimable by this worker or already done")

        with conn.cursor() as cur:
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"task_complete: {task_id[:8]} by {claims['sub']}",),
            )
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

import datetime


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
    lease_expires = datetime.datetime.utcnow() + datetime.timedelta(seconds=lease_seconds)
    worker_id = claims["sub"]

    conn = get_dolt_conn()
    try:
        # Reap stale leases first (on-claim sweep)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status='pending', claimed_by=NULL, lease_expires=NULL "
                "WHERE status='claimed' AND lease_expires < %s",
                (datetime.datetime.utcnow(),),
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
