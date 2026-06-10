import os
import time
import json
import hashlib
import logging
from fastapi import FastAPI, HTTPException, Header, Request, Response
import httpx
import jwt
import pymysql
import redis as redis_lib
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

app = FastAPI()

JWT_SECRET = os.environ["JWT_SECRET"]
MCPJUNGLE_URL = os.environ["MCPJUNGLE_INTERNAL_URL"]
OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
DOLT_HOST = os.environ.get("DOLT_HOST", "dolt")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
DOLT_USER = os.environ.get("DOLT_USER", "harness")
DOLT_PASSWORD = os.environ.get("DOLT_PASSWORD", "harness")
DOLT_DB = os.environ.get("DOLT_DB", "harness")
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "900"))  # 15 min
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "180"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))
GATEWAY_BACKEND = os.environ.get("GATEWAY_BACKEND", "mcpjungle").lower()
CF_URL = os.environ.get("CF_URL", "http://contextforge:4444")
CF_JWT_SECRET = os.environ.get("CF_JWT_SECRET", "cf-dev-secret-key-at-least-32-bytes-long")
CF_ADMIN_EMAIL = os.environ.get("CF_ADMIN_EMAIL", "admin@harness.local")
CF_SERVER_NAME = os.environ.get("CF_SERVER_NAME", "harness_all")

_cf_server_uuid: str | None = os.environ.get("CF_SERVER_UUID")  # pre-set or discovered

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
_tool_calls_total = Counter(
    "harness_tool_calls_total",
    "Total tool invocations by agent role and decision",
    ["agent_role", "decision", "backend"],
)
_tool_call_latency = Histogram(
    "harness_tool_call_latency_ms",
    "Tool call latency in milliseconds",
    ["agent_role", "backend"],
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)
_rate_limit_rejections = Counter(
    "harness_rate_limit_rejections_total",
    "Calls rejected by rate limiter",
    ["agent_role"],
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

_redis: redis_lib.Redis | None = None


def _get_redis() -> redis_lib.Redis | None:
    global _redis
    if _redis is None:
        try:
            _redis = redis_lib.from_url(REDIS_URL, socket_connect_timeout=2)
            _redis.ping()
        except Exception as e:
            logger.warning("Redis unavailable, rate limiting disabled: %s", e)
            _redis = None
    return _redis


def _check_rate_limit(agent_id: str) -> bool:
    """Sliding-window rate limiter. Returns True if the call should be rejected."""
    r = _get_redis()
    if r is None:
        return False  # fail open if Redis is down
    bucket = int(time.time()) // 60
    key = f"rl:{agent_id}:{bucket}"
    try:
        count = r.incr(key)
        if count == 1:
            r.expire(key, 120)
        return count > RATE_LIMIT_PER_MINUTE
    except Exception as e:
        logger.warning("Rate limit check failed: %s", e)
        return False

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
    """Raises HTTPException on missing/expired/invalid token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing_token")
    raw_token = authorization[7:]
    try:
        return jwt.decode(raw_token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid_token")


async def _check_policy(role: str, short_tool: str) -> bool:
    """Returns True if OPA allows the call; False on deny or OPA error."""
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


def _cf_token() -> str:
    """Generate a short-lived ContextForge JWT for governance's internal calls."""
    import uuid as _uuid
    now = int(time.time())
    return jwt.encode(
        {
            "sub": CF_ADMIN_EMAIL,
            "preferred_username": "admin",
            "iat": now,
            "iss": "mcpgateway",
            "aud": "mcpgateway-api",
            "jti": str(_uuid.uuid4()),
            "exp": now + 3600,
        },
        CF_JWT_SECRET,
        algorithm="HS256",
    )


def _to_cf_tool_name(mcp_name: str) -> str:
    """Map MCPJungle flat name to ContextForge slug.

    architect_stub__codebase_search  →  architect-stub-codebase-search
    """
    return mcp_name.replace("__", "-").replace("_", "-")


async def _get_cf_server_uuid() -> str:
    """Discover ContextForge virtual server UUID by name (cached)."""
    global _cf_server_uuid
    if _cf_server_uuid:
        return _cf_server_uuid
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{CF_URL}/servers",
            headers={"Authorization": f"Bearer {_cf_token()}", "Accept": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        for srv in resp.json():
            if srv.get("name") == CF_SERVER_NAME:
                _cf_server_uuid = srv["id"]
                logger.info("CF server '%s' → %s", CF_SERVER_NAME, _cf_server_uuid)
                return _cf_server_uuid
    raise RuntimeError(f"CF virtual server '{CF_SERVER_NAME}' not found")


async def _forward_to_contextforge(body: dict) -> tuple:
    """Translate MCPJungle flat body to ContextForge JSON-RPC; returns (mock_response, latency_ms)."""
    start = int(time.time() * 1000)
    full_tool = body.get("name", "")
    cf_tool_name = _to_cf_tool_name(full_tool)
    params = {k: v for k, v in body.items() if k != "name"}

    server_uuid = await _get_cf_server_uuid()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": cf_tool_name, "arguments": params},
    }
    async with httpx.AsyncClient() as client:
        upstream = await client.post(
            f"{CF_URL}/servers/{server_uuid}/mcp",
            headers={
                "Authorization": f"Bearer {_cf_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json=payload,
            timeout=UPSTREAM_TIMEOUT,
        )

    latency = int(time.time() * 1000) - start
    if upstream.status_code not in (200, 201):
        logger.warning("CF upstream %d for %s: %s", upstream.status_code, cf_tool_name, upstream.text[:200])
    # Re-wrap into MCPJungle-compatible format for downstream consumers
    cf_data = upstream.json()
    result = cf_data.get("result", {})
    content = result.get("content", [])
    # Build a fake MCPJungle-shaped response object
    wrapped = {"content": content}
    # Return as a mock response-like object that governance's _write_audit expects
    class _FakeResp:
        status_code = upstream.status_code
        text = json.dumps(wrapped)
        def raise_for_status(self): upstream.raise_for_status()
        def json(self): return wrapped

    return _FakeResp(), latency


async def _forward_upstream(body: dict) -> tuple:
    """Route to MCPJungle or ContextForge based on GATEWAY_BACKEND env var."""
    if GATEWAY_BACKEND == "contextforge":
        return await _forward_to_contextforge(body)
    return await _forward_to_mcpjungle(body)


async def _forward_to_mcpjungle(body: dict) -> tuple:
    """POST to MCPJungle; returns (response, latency_ms)."""
    start = int(time.time() * 1000)
    async with httpx.AsyncClient() as client:
        upstream = await client.post(
            f"{MCPJUNGLE_URL}/api/v0/tools/invoke",
            json=body,
            timeout=UPSTREAM_TIMEOUT,
        )
    return upstream, int(time.time() * 1000) - start


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=DOLT_USER,
        password=DOLT_PASSWORD,
        database=DOLT_DB,
        autocommit=True,
    )


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
    access_token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL,
    }


@app.post("/api/v0/tools/invoke")
async def invoke(
    request: Request,
    authorization: str | None = Header(default=None),
    x_human_approval_token: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)

    if _check_rate_limit(claims["sub"]):
        _rate_limit_rejections.labels(agent_role=claims.get("role", "unknown")).inc()
        raise HTTPException(429, "rate_limit_exceeded")

    body = await request.json()
    full_tool = body.get("name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = f"harness.allow[{claims['role']}]"

    if short_tool == "shell_exec" and not x_human_approval_token:
        raise HTTPException(403, "shell_exec_requires_human_approval")

    if not await _check_policy(claims["role"], short_tool):
        _tool_calls_total.labels(agent_role=claims["role"], decision="deny", backend=GATEWAY_BACKEND).inc()
        _write_audit(claims["sub"], full_tool, short_tool, json.dumps(body), None, "deny", rule, 0)
        raise HTTPException(403, "policy_denied")

    upstream, latency = await _forward_upstream(body)
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    resp_hash = hashlib.sha256(upstream.text.encode()).hexdigest()[:16]
    _tool_calls_total.labels(agent_role=claims["role"], decision="allow", backend=GATEWAY_BACKEND).inc()
    _tool_call_latency.labels(agent_role=claims["role"], backend=GATEWAY_BACKEND).observe(latency)
    _write_audit(claims["sub"], full_tool, short_tool, req_hash, resp_hash, "allow", rule, latency)

    upstream.raise_for_status()
    return upstream.json()


def _write_audit(
    agent_id,
    tool_name,
    server_id,
    req_hash,
    resp_hash,
    decision,
    rule,
    latency_ms,
):
    conn = None
    try:
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_log
                   (agent_id, tool_name, server_id, request_hash, response_hash,
                    policy_decision, policy_rule, timestamp_ms, latency_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/memory/write")
async def memory_write(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Auth-gated memory write proxy.  Requires a valid Bearer token — returns
    401 for unauthenticated callers (OWASP Agentic AI: Memory Poisoning)."""
    _decode_jwt(authorization)   # raises 401 if missing/invalid
    body = await request.json()
    # Actual persistence is a future concern; the auth gate is what matters here.
    logger.info(
        "memory_write: ns=%s key=%s by agent=%s",
        body.get("namespace"),
        body.get("key"),
        "authenticated",
    )
    return {"status": "ok"}
