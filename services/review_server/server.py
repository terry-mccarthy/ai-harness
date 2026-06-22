import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from typing import Any

import asyncpg
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState
from metrics import MonitoredLLMProvider

_PG_POOL: asyncpg.Pool | None = None


async def _init_pg_pool() -> None:
    global _PG_POOL
    dsn = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")
    if not dsn:
        return
    for attempt in range(1, 6):
        try:
            _PG_POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
            logging.info("connected to postgres config store")
            return
        except Exception:
            if attempt < 5:
                wait = attempt * 2
                logging.warning(
                    "pg connect attempt %d/5 failed, retrying in %ds...", attempt, wait
                )
                await asyncio.sleep(wait)
            else:
                logging.warning(
                    "config persistence unavailable — pg not reachable after 5 attempts",
                    exc_info=True,
                )


async def _close_pg_pool() -> None:
    global _PG_POOL
    if _PG_POOL:
        await _PG_POOL.close()
        _PG_POOL = None


async def _ensure_config_table() -> None:
    if not _PG_POOL:
        return
    async with _PG_POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                config     JSONB NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT single_row CHECK (id = 1)
            )
        """)
        await conn.execute("""
            INSERT INTO server_config (id, config)
            VALUES (1, '{}')
            ON CONFLICT (id) DO NOTHING
        """)


def _apply_config_overrides(overrides: dict) -> None:
    if "llm_provider" in overrides:
        _CONFIG["llm_provider"] = overrides["llm_provider"]
    for prov in ("ollama", "gemini", "openrouter"):
        if prov in overrides:
            _CONFIG[prov] = overrides[prov]


async def _load_config_from_pg() -> None:
    if not _PG_POOL:
        return
    async with _PG_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT config FROM server_config WHERE id = 1")
    if row and row["config"]:
        _apply_config_overrides(json.loads(row["config"]))


async def _save_config_to_pg() -> None:
    if not _PG_POOL:
        return
    payload: dict = {}
    if _CONFIG["llm_provider"] is not None:
        payload["llm_provider"] = _CONFIG["llm_provider"]
    for prov in ("ollama", "gemini", "openrouter"):
        if _CONFIG.get(prov):
            payload[prov] = _CONFIG[prov]
    async with _PG_POOL.acquire() as conn:
        await conn.execute(
            "UPDATE server_config SET config = $1::jsonb, updated_at = now() WHERE id = 1",
            json.dumps(payload),
        )


@asynccontextmanager
async def lifespan(server):
    await _init_pg_pool()
    await _ensure_config_table()
    await _load_config_from_pg()
    if _PG_POOL:
        logging.info("loaded runtime config from postgres")
    yield
    await _close_pg_pool()


mcp = FastMCP(
    "review_server",
    host="0.0.0.0",
    port=9003,
    lifespan=lifespan,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_DEFAULT_TASK = (
    "Review this diff for: "
    "(1) security vulnerabilities — credential leaks, injection flaws, path traversal, missing auth enforcement, insecure defaults; "
    "(2) code quality — error handling gaps, dead code, resource leaks, incorrect types, silent failures; "
    "(3) architectural concerns — hardcoded values, tight coupling, shared mutable state, missing abstractions. "
    "Report every finding with file, line, severity (CRITICAL/WARNING/INFO), and a specific fix suggestion. "
    "Verdict is 'fail' if any CRITICAL finding exists."
)

# ---------------------------------------------------------------------------
# Runtime config store — set via PUT /config, persisted in postgres server_config
# table. Loaded at startup via lifespan hook; saved on every PUT /config.
# Each provider sub-dict holds typed overrides that take precedence over env vars.
# Setting a value to None (or omitting the key) falls through to the env var / default.
# ---------------------------------------------------------------------------
_CONFIG: dict = {
    "llm_provider": None,   # overrides LLM_PROVIDER env var
    "ollama": {},
    "gemini": {},
    "openrouter": {},
}

_SENSITIVE_KEYS = {"api_key", "client_secret", "secret"}


_ENV_CFG = {
    "llm_provider": ("LLM_PROVIDER", "ollama"),
    "ollama": {
        "model": ("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "host": ("OLLAMA_HOST", "http://localhost:11434"),
        "num_ctx": ("OLLAMA_NUM_CTX", 8192),
        "temperature": ("OLLAMA_TEMPERATURE", 0.1),
        "num_predict": ("OLLAMA_NUM_PREDICT", 1024),
    },
    "gemini": {
        "model": ("GEMINI_MODEL", "gemini-2.5-flash"),
        "api_key": ("GEMINI_API_KEY", None),
    },
    "openrouter": {
        "model": ("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        "api_key": ("OPENROUTER_API_KEY", None),
    },
}


def _env_cfg() -> dict:
    result = {}
    result["llm_provider"] = _CONFIG.get("llm_provider") or os.environ.get("LLM_PROVIDER", "ollama")
    for provider in ("ollama", "gemini", "openrouter"):
        sub = {}
        for k, (env_var, default) in _ENV_CFG[provider].items():
            sub[k] = _get_cfg(provider, k) or os.environ.get(env_var, default)
        result[provider] = sub
    return result


def _should_mask(key: str, val) -> bool:
    if key.lower() not in _SENSITIVE_KEYS:
        return False
    if not isinstance(val, str):
        return False
    if not val:
        return False
    return True


def _mask_value(val: str) -> str:
    return val[:4] + "..." if len(val) > 8 else "***"


def _sanitize_cfg(val, key=""):
    """Mask sensitive values (api keys, secrets) for display."""
    if isinstance(val, dict):
        return {k: _sanitize_cfg(v, k) for k, v in val.items()}
    if _should_mask(key, val):
        return _mask_value(val)
    return val


def _get_cfg(provider: str, key: str):
    """Return a config override value, or None if not set."""
    prov = _CONFIG.get(provider, {})
    if not isinstance(prov, dict):
        return None
    return prov.get(key)


def _resolve(override, provider: str, key: str, env_var: str, default, *, cast=None):
    if override is not None:
        return override
    cfg = _get_cfg(provider, key)
    if cfg is not None:
        return cfg
    raw = os.environ.get(env_var)
    if raw is not None:
        if cast:
            try:
                return cast(raw)
            except (ValueError, TypeError):
                logging.warning("Invalid value for %s: %r, using default %r", env_var, raw, default)
        else:
            return raw
    return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        logging.warning("Invalid value for %s, using default %s", key, default)
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        logging.warning("Invalid value for %s, using default %s", key, default)
        return default


def _build_gemini_provider(**kwargs):
    from harness_agents.llm import GeminiProvider
    api_key = _resolve(None, "gemini", "api_key", "GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required for the gemini provider")
    return GeminiProvider(
        model=_resolve(kwargs.get("model"), "gemini", "model", "GEMINI_MODEL", "gemini-2.5-flash"),
        api_key=api_key,
        temperature=_resolve(kwargs.get("temperature"), "gemini", "temperature", "LLM_TEMPERATURE", 0.1, cast=float),
        max_output_tokens=_resolve(kwargs.get("max_tokens"), "gemini", "max_output_tokens", "LLM_MAX_TOKENS", 1024, cast=int),
    )


def _build_openrouter_provider(**kwargs):
    from harness_agents.llm import OpenRouterProvider
    api_key = _resolve(None, "openrouter", "api_key", "OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required for the openrouter provider")
    return OpenRouterProvider(
        api_key=api_key,
        model=_resolve(kwargs.get("model"), "openrouter", "model", "OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        temperature=_resolve(kwargs.get("temperature"), "openrouter", "temperature", "LLM_TEMPERATURE", 0.1, cast=float),
        max_tokens=_resolve(kwargs.get("max_tokens"), "openrouter", "max_tokens", "LLM_MAX_TOKENS", 1024, cast=int),
    )


def _build_ollama_provider(host=None, model=None, temperature=None, max_tokens=None, num_ctx=None, num_predict=None):
    from harness_agents.llm import OllamaProvider
    return OllamaProvider(
        host=_resolve(host, "ollama", "host", "OLLAMA_HOST", "http://localhost:11434"),
        model=_resolve(model, "ollama", "model", "OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=_resolve(num_ctx, "ollama", "num_ctx", "OLLAMA_NUM_CTX", 8192, cast=int),
        temperature=_resolve(
            temperature, "ollama", "temperature", "LLM_TEMPERATURE",
            _env_float("OLLAMA_TEMPERATURE", 0.1),
            cast=float,
        ),
        num_predict=_resolve(
            num_predict, "ollama", "num_predict", "LLM_MAX_TOKENS",
            _env_int("OLLAMA_NUM_PREDICT", 1024),
            cast=int,
        ),
    )


_BUILDERS = {
    "gemini": _build_gemini_provider,
    "openrouter": _build_openrouter_provider,
    "ollama": _build_ollama_provider,
}


def _build_llm_provider(provider_name: str, **kwargs):
    """Factory: return a concrete LLMProvider. Resolution: kwarg > config > env > default."""
    builder = _BUILDERS.get(provider_name)
    if not builder:
        raise ValueError(f"Unknown LLM provider: {provider_name!r}. Supported: ollama, gemini, openrouter")
    return builder(**kwargs)


async def _run_review(
    diff_text: str,
    task: str,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Run the CodeReviewerAgent and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    gateway = GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id="code-reviewer",
        client_secret=os.environ.get("CODE_REVIEWER_SECRET", ""),
    )
    resolved_provider = (
        provider
        or _CONFIG.get("llm_provider")
        or os.environ.get("LLM_PROVIDER", "ollama")
    ).lower()
    llm_provider = _build_llm_provider(
        resolved_provider,
        host=host,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )
    llm_provider = MonitoredLLMProvider(llm_provider, agent_role="code_reviewer")
    agent = CodeReviewerAgent(gateway=gateway, llm_provider=llm_provider)
    state = AgentState(
        task=task,
        diff=diff_text,
        thread_id="mcp-call",
        agent_output=None,
        requires_human_approval=False,
        error=None,
    )
    result = await agent.run(state)
    if result.get("error"):
        raise ValueError(result["error"]["reason"])
    return result["agent_output"]


# ---------------------------------------------------------------------------
# MCP tool: review_diff
# ---------------------------------------------------------------------------

@mcp.tool()
async def review_diff(
    diff_text: str,
    provider: str | None = None,
    task: str = _DEFAULT_TASK,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Run the governed code-reviewer agent and return structured findings.

    Args:
        diff_text: The unified diff string to review.
        provider: Optional LLM provider override (``"ollama"``, ``"gemini"``, or ``"openrouter"``).
            Falls back to the ``LLM_PROVIDER`` environment variable, then the runtime config.
        task: High-level review instruction passed to the agent.
        model: Override the model name for the resolved provider.
        temperature: Override the temperature setting.
        max_tokens: Override the max tokens / max output tokens / num_predict setting.
        num_ctx: Override the context window (Ollama only).
        num_predict: Override num_predict (Ollama only).
        host: Override the Ollama host URL.
    """
    try:
        return await _run_review(
            diff_text, task, provider,
            model=model, temperature=temperature, max_tokens=max_tokens,
            num_ctx=num_ctx, num_predict=num_predict, host=host,
        )
    except Exception as e:
        logging.exception("review_diff failed")
        raise RuntimeError(str(e)) from e


# ---------------------------------------------------------------------------
# HTTP endpoint: POST /review
# ---------------------------------------------------------------------------

def _check_api_key(request: Request) -> bool:
    """Return True if the request is authorised.

    When REVIEW_API_KEY is unset the endpoint is open (dev/local mode).
    When set, the request must carry 'Authorization: Bearer <key>'.
    """
    required = os.environ.get("REVIEW_API_KEY")
    if not required:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return header[len("Bearer "):] == required


@mcp.custom_route("/review", methods=["POST"])
async def http_review(request: Request) -> JSONResponse:
    """Plain HTTP endpoint for CI pipelines, pre-commit hooks, and webhooks.

    Body (JSON):
        diff_text   (str, required): unified diff to review
        task        (str, optional): review instruction
        provider    (str, optional): provider name override
        model       (str, optional): model name override
        temperature (float, optional): temperature override
        max_tokens  (int, optional): max tokens override
        num_ctx     (int, optional): context window override (Ollama)
        num_predict (int, optional): num_predict override (Ollama)
        host        (str, optional): Ollama host override

    Auth: set REVIEW_API_KEY in env to require 'Authorization: Bearer <key>'.
    When REVIEW_API_KEY is unset the endpoint is open (dev/local mode).

    Returns the same structured findings as the MCP ``review_diff`` tool.
    """
    if not _check_api_key(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=422)

    diff_text = body.get("diff_text")
    if not diff_text:
        return JSONResponse({"error": "diff_text is required"}, status_code=422)

    task = body.get("task", _DEFAULT_TASK)
    provider = body.get("provider")

    try:
        findings = await _run_review(
            diff_text, task, provider,
            model=body.get("model"),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            num_ctx=body.get("num_ctx"),
            num_predict=body.get("num_predict"),
            host=body.get("host"),
        )
        return JSONResponse(findings)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logging.exception("review failed")
        return JSONResponse({"error": "review failed — see server logs"}, status_code=500)


# ---------------------------------------------------------------------------
# Config API — read / write runtime overrides
# ---------------------------------------------------------------------------

@mcp.custom_route("/config", methods=["GET"])
async def get_config(request: Request) -> JSONResponse:
    """Return effective runtime config (env vars + overrides, secrets masked)."""
    if not _check_api_key(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(_sanitize_cfg(_env_cfg()))


async def _parse_json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body


def _update_provider_config(prov: str, overrides: Any) -> None:
    if not isinstance(overrides, dict):
        return
    _CONFIG.setdefault(prov, {})
    for k, v in overrides.items():
        if v is None:
            _CONFIG[prov].pop(k, None)
        else:
            _CONFIG[prov][k] = v


@mcp.custom_route("/config", methods=["PUT"])
async def put_config(request: Request) -> JSONResponse:
    if not _check_api_key(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await _parse_json_body(request)
    if body is None:
        return JSONResponse({"error": "invalid JSON body"}, status_code=422)

    if "llm_provider" in body:
        _CONFIG["llm_provider"] = body["llm_provider"]

    for prov in ("ollama", "gemini", "openrouter"):
        _update_provider_config(prov, body.get(prov))

    await _save_config_to_pg()

    return JSONResponse({"status": "ok", "config": _sanitize_cfg(_CONFIG)})


# ---------------------------------------------------------------------------
# MCP tool: run_skill (unchanged)
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_skill(
    skill_id: str,
    inputs: dict | None = None,
) -> dict:
    """Execute a promoted governed skill by ID, running each step through OPA.

    Args:
        skill_id: The skill identifier (e.g. ``"sre:triage-incident"``).
        inputs: Optional input parameters passed to each step.
    """
    gateway = GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id=os.environ.get("SKILL_CLIENT_ID", "sre"),
        client_secret=os.environ.get("SKILL_CLIENT_SECRET", os.environ.get("SRE_SECRET", "")),
    )
    try:
        return await gateway.execute_skill(skill_id, inputs)
    except Exception as e:
        logging.exception("run_skill failed for %s", skill_id)
        raise RuntimeError(str(e)) from e


# ---------------------------------------------------------------------------
# MCP tool: architecture_review
# ---------------------------------------------------------------------------

@mcp.tool()
async def architecture_review(
    target_mode: str,
    repo: str,
    diff: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Score a codebase or diff against the repo's stated architectural invariants.

    Fetches ``ARCHITECTURE.md`` and ADRs from the GitHub repo via the GitHub API,
    then scores the codebase file tree (``target_mode="codebase"``) or a unified
    diff (``target_mode="diff"``) against the stated invariants.

    Args:
        target_mode: ``"codebase"`` (scan file tree) or ``"diff"`` (score a unified diff).
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        diff: Unified diff text (required when ``target_mode="diff"``).
        provider: Optional LLM provider override.
        model: Override the model name.
        temperature: Override the temperature setting.
        max_tokens: Override the max tokens / max output tokens / num_predict setting.
        num_ctx: Override the context window (Ollama only).
        num_predict: Override num_predict (Ollama only).
        host: Override the Ollama host URL.
    """
    from architecture_review import architecture_review as _architecture_review

    resolved_provider = (
        provider
        or _CONFIG.get("llm_provider")
        or os.environ.get("LLM_PROVIDER", "ollama")
    ).lower()
    llm_provider = _build_llm_provider(
        resolved_provider,
        host=host,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )
    llm_provider = MonitoredLLMProvider(llm_provider, agent_role="architect")
    try:
        return await _architecture_review(
            repo=repo,
            target_mode=target_mode,
            diff=diff,
            llm_provider=llm_provider,
        )
    except Exception as e:
        logging.exception("architecture_review failed")
        raise RuntimeError(str(e)) from e


# ---------------------------------------------------------------------------
# Architecture review HTTP endpoint (no MCP timeout limit)
# ---------------------------------------------------------------------------


@mcp.custom_route("/review-architecture", methods=["POST"])
async def http_architecture_review(request: Request) -> JSONResponse:
    """Plain HTTP endpoint for architecture review — no MCP client timeout.

    Body (JSON):
        target_mode (str, required): ``"codebase"`` or ``"diff"``
        repo        (str, required): GitHub URL
        diff        (str, optional): unified diff (required when target_mode="diff")
        provider    (str, optional): provider name override
        model       (str, optional): model name override
        temperature (float, optional): temperature override
        max_tokens  (int, optional): max tokens override
        num_ctx     (int, optional): context window override (Ollama)
        num_predict (int, optional): num_predict override (Ollama)
        host        (str, optional): Ollama host override

    Auth: set REVIEW_API_KEY in env (same as POST /review).
    """
    if not _check_api_key(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=422)

    target_mode = body.get("target_mode")
    if not target_mode:
        return JSONResponse({"error": "target_mode is required"}, status_code=422)
    repo = body.get("repo")
    if not repo:
        return JSONResponse({"error": "repo is required"}, status_code=422)

    from architecture_review import architecture_review as _architecture_review

    resolved_provider = (
        body.get("provider")
        or _CONFIG.get("llm_provider")
        or os.environ.get("LLM_PROVIDER", "ollama")
    ).lower()
    try:
        llm_provider = _build_llm_provider(
            resolved_provider,
            host=body.get("host"),
            model=body.get("model"),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            num_ctx=body.get("num_ctx"),
            num_predict=body.get("num_predict"),
        )
        llm_provider = MonitoredLLMProvider(llm_provider, agent_role="architect")
        result = await _architecture_review(
            repo=repo,
            target_mode=target_mode,
            diff=body.get("diff"),
            llm_provider=llm_provider,
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logging.exception("architecture_review failed")
        return JSONResponse({"error": "architecture review failed — see server logs"}, status_code=500)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_route(request: Request) -> Response:
    """Prometheus metrics endpoint, scraped by the monitoring stack."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# MCP tool: execute_architecture_check (unchanged)
# ---------------------------------------------------------------------------

@mcp.tool()
async def execute_architecture_check(
    target_language: str,
    repo_path: str,
) -> dict:
    """Execute static analysis checks on the target codebase and return a GateSignalContract.

    Args:
        target_language: The programming language of the codebase (e.g., ``'python'``, ``'php'``, ``'typescript'``).
        repo_path: The directory path or GitHub URL of the codebase to analyze.
    """
    from architecture_gate.runner import run_gate

    logging.info(
        "execute_architecture_check called for lang=%s repo=%s",
        target_language,
        repo_path,
    )
    signal = await run_gate(repo_path, target_language)
    return signal.to_dict()


# ---------------------------------------------------------------------------
# MCP tools: code-forensics style analysis
# ---------------------------------------------------------------------------


@mcp.tool()
async def code_health_score(
    file_paths: list[str],
    repo: str,
) -> list[dict]:
    """Analyze code health (complexity, function length) for specific files in a GitHub repo.

    Fetches each file from the GitHub API and runs radon cyclomatic
    complexity analysis. Returns scores 0-10 (higher = healthier),
    sorted worst-first.

    Args:
        file_paths: List of file paths relative to the repo root (e.g. ``["src/main.py", "lib/utils.ts"]``).
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
    """
    from code_analysis import get_code_health as _get_code_health

    try:
        return await _get_code_health(file_paths, repo)
    except Exception as e:
        logging.exception("code_health_score failed")
        return [{"error": str(e)}]


@mcp.tool()
async def codebase_hotspots(
    repo: str,
    top_n: int = 10,
    language: str | None = None,
) -> list[dict]:
    """Rank files in a GitHub repo by complexity-based hotspot risk.

    Fetches the file tree from the GitHub API, downloads source files,
    and ranks them by cyclomatic complexity. High-complexity files
    are hotspots most likely to contain bugs.

    Args:
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        top_n: Number of top hotspots to return (default 10).
        language: Optional language filter (e.g. ``"python"``, ``"typescript"``).
    """
    from code_analysis import get_hotspots as _get_hotspots

    try:
        return await _get_hotspots(repo, top_n=top_n, language=language)
    except Exception as e:
        logging.exception("codebase_hotspots failed")
        return [{"error": str(e)}]


@mcp.tool()
async def logical_coupling(
    repo: str,
    file_path: str,
    max_commits: int = 50,
) -> list[dict]:
    """Find files that historically change together with a given file.

    Uses the GitHub commits API to find recent commits touching
    ``file_path``, then extracts all other files changed in those
    commits. Returns the co-changing files ranked by frequency.

    Args:
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        file_path: Path to the file to analyse (e.g. ``"src/main.py"``).
        max_commits: Maximum recent commits to inspect (default 50).
    """
    from code_analysis import get_logical_coupling as _get_logical_coupling

    try:
        return await _get_logical_coupling(repo, file_path, max_commits=max_commits)
    except Exception as e:
        logging.exception("logical_coupling failed")
        return [{"error": str(e)}]


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9003)
