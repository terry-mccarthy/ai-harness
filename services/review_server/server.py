from contextlib import asynccontextmanager
import logging
import os
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from harness_gateway.client import GatewayClient
from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic
from harness_agents.types import AgentState
from metrics import MonitoredLLMProvider
from core.config import (
    _CONFIG,
    _check_api_key,
    _close_pg_pool,
    _ensure_config_table,
    _env_cfg,
    _get_cfg,
    _init_pg_pool,
    _load_config_from_pg,
    _pg_pool_connected,
    _sanitize_cfg,
    _save_config_to_pg,
)
from services.code_analysis import (
    _DEFAULT_ADVERSARIAL_TASK,
    _build_llm_provider,
    _chain_adversarial_verdict,
    _run_adversarial_review as _ca_run_adversarial_review,
    _run_review_maybe_chained as _ca_run_review_maybe_chained,
)


@asynccontextmanager
async def lifespan(server):
    await _init_pg_pool()
    await _ensure_config_table()
    await _load_config_from_pg()
    if _pg_pool_connected():
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

_DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK = (
    "Attack the first-pass architect's synthesis findings. Confirm, refute, escalate, "
    "downgrade, or leave unresolved each one. A confirmed or escalated HIGH/CRITICAL "
    "finding requires a concrete regression_scenario — a specific failure trace grounded "
    "in the codebase, not a restatement of the severity."
)

# ---------------------------------------------------------------------------
# MCP tool: review_diff
#
# The LLM-provider factory and the _run_review*/_run_chained_review
# orchestration now live in services/code_analysis.py (issue #07). The thin
# wrapper below exists only so that unittest.mock.patch.object(review_server,
# "GatewayClient" / "_build_llm_provider", ...) (used throughout
# packages/harness-tests/) keeps working: those names are resolved as bare
# globals *inside this module* at call time, so this wrapper reads its own
# (possibly patched) current values and threads them into
# services.code_analysis explicitly, rather than letting the moved functions
# silently fall back to their own module's unpatched originals.
# ---------------------------------------------------------------------------


async def _run_review_maybe_chained(
    diff_text: str,
    task: str,
    provider: str | None,
    chain_adversarial: bool,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Shared branch point for review_diff / POST /review.

    chain_adversarial=False (the default) returns exactly what _run_review
    returns — byte-for-byte identical to today's response. chain_adversarial=True
    returns the combined {"first_pass", "critic", "verdict"} shape from
    _run_chained_review.
    """
    return await _ca_run_review_maybe_chained(
        diff_text, task, provider, chain_adversarial,
        model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
        gateway_client_cls=GatewayClient, build_llm_provider=_build_llm_provider,
    )


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
    chain_adversarial: bool = False,
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
        chain_adversarial: When ``True``, feed the first-pass output into the
            adversarial code critic and return ``{"first_pass", "critic", "verdict"}``
            instead of the plain first-pass findings. Defaults to ``False``, which
            is byte-for-byte identical to today's response.
    """
    try:
        return await _run_review_maybe_chained(
            diff_text, task, provider, chain_adversarial,
            model=model, temperature=temperature, max_tokens=max_tokens,
            num_ctx=num_ctx, num_predict=num_predict, host=host,
        )
    except Exception as e:
        logging.exception("review_diff failed")
        raise RuntimeError(str(e)) from e


# ---------------------------------------------------------------------------
# HTTP endpoint: POST /review
# ---------------------------------------------------------------------------


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
        chain_adversarial (bool, optional): when true, feed the first-pass output into
            the adversarial code critic and return ``{"first_pass", "critic", "verdict"}``
            instead of the plain first-pass findings. Defaults to false, which is
            byte-for-byte identical to today's response.

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
    chain_adversarial = body.get("chain_adversarial", False)

    try:
        result = await _run_review_maybe_chained(
            diff_text, task, provider, chain_adversarial,
            model=body.get("model"),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            num_ctx=body.get("num_ctx"),
            num_predict=body.get("num_predict"),
            host=body.get("host"),
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logging.exception("review failed")
        return JSONResponse({"error": "review failed — see server logs"}, status_code=500)


# ---------------------------------------------------------------------------
# MCP tool: adversarial_review — attacks the first-pass review_diff output
#
# _run_adversarial_review's real implementation (GatewayClient + LLM-provider
# construction, AdversarialCodeCritic invocation) now lives in
# services/code_analysis.py (issue #07). This thin wrapper exists so that (a)
# tests patching review_server.GatewayClient / review_server._build_llm_provider
# still take effect (see the _run_review_maybe_chained wrapper above for why),
# and (b) packages/harness-tests/test_adversarial_review_http.py, which calls
# review_server._run_adversarial_review(...) directly (bypassing the
# adversarial_review tool entirely), keeps working unchanged.
# ---------------------------------------------------------------------------


async def _run_adversarial_review(
    diff_text: str,
    first_pass_output: dict,
    task: str,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Run the AdversarialCodeCritic and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    return await _ca_run_adversarial_review(
        diff_text, first_pass_output, task, provider,
        model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
        gateway_client_cls=GatewayClient, build_llm_provider=_build_llm_provider,
    )


@mcp.tool()
async def adversarial_review(
    diff_text: str,
    first_pass_output: dict,
    provider: str | None = None,
    task: str = _DEFAULT_ADVERSARIAL_TASK,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Attack a first-pass review_diff output and return confirm/refute/escalate findings.

    Args:
        diff_text: The unified diff string that was reviewed.
        first_pass_output: The structured output of a prior review_diff call.
        provider: Optional LLM provider override (``"ollama"``, ``"gemini"``, or ``"openrouter"``).
        task: High-level instruction passed to the critic.
        model: Override the model name for the resolved provider.
        temperature: Override the temperature setting.
        max_tokens: Override the max tokens / max output tokens / num_predict setting.
        num_ctx: Override the context window (Ollama only).
        num_predict: Override num_predict (Ollama only).
        host: Override the Ollama host URL.
    """
    try:
        return await _run_adversarial_review(
            diff_text, first_pass_output, task, provider,
            model=model, temperature=temperature, max_tokens=max_tokens,
            num_ctx=num_ctx, num_predict=num_predict, host=host,
        )
    except Exception as e:
        logging.exception("adversarial_review failed")
        raise RuntimeError(str(e)) from e


@mcp.custom_route("/review-adversarial", methods=["POST"])
async def http_adversarial_review(request: Request) -> JSONResponse:
    """Plain HTTP endpoint for the adversarial code critic.

    Body (JSON):
        diff_text         (str, required): unified diff that was reviewed
        first_pass_output (dict, required): structured output of a prior review_diff call
        task               (str, optional): critique instruction
        provider, model, temperature, max_tokens, num_ctx, num_predict, host: same as POST /review

    Auth: set REVIEW_API_KEY in env (same as POST /review).

    Returns the AdversarialCodeCritic's structured findings.
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

    first_pass_output = body.get("first_pass_output")
    if first_pass_output is None:
        return JSONResponse({"error": "first_pass_output is required"}, status_code=422)

    task = body.get("task", _DEFAULT_ADVERSARIAL_TASK)
    provider = body.get("provider")

    try:
        findings = await _run_adversarial_review(
            diff_text, first_pass_output, task, provider,
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
        logging.exception("adversarial review failed")
        return JSONResponse({"error": "adversarial review failed — see server logs"}, status_code=500)


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

_ARCHITECTURE_CHAIN_FAIL_SEVERITIES = {"CRITICAL", "HIGH"}


async def _run_architecture_review_chain(
    *,
    repo: str,
    target_mode: str,
    diff: str | None,
    llm_provider,
    chain_adversarial: bool,
    provider: str | None,
    model: str | None,
    temperature: float | None,
    max_tokens: int | None,
    num_ctx: int | None,
    num_predict: int | None,
    host: str | None,
) -> dict:
    """Run the first-pass architecture synthesis and, when requested, chain the
    adversarial architecture critic on top of it.

    Returns the first-pass output unchanged when ``chain_adversarial`` is False.
    Otherwise returns ``{"first_pass", "critic", "verdict"}`` where verdict is
    computed per the confirmed/escalated-only-fails rule (HIGH+ threshold).
    """
    from architecture_review import architecture_review as _architecture_review

    first_pass_output = await _architecture_review(
        repo=repo,
        target_mode=target_mode,
        diff=diff,
        llm_provider=llm_provider,
    )
    if not chain_adversarial:
        return first_pass_output

    critic_output = await _run_adversarial_architecture_review(
        repo, first_pass_output, _DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK, provider,
        diff=diff, model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
    )
    verdict = _chain_adversarial_verdict(
        critic_output.get("findings", []), _ARCHITECTURE_CHAIN_FAIL_SEVERITIES
    )
    return {"first_pass": first_pass_output, "critic": critic_output, "verdict": verdict}


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
    chain_adversarial: bool = False,
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
        chain_adversarial: When ``True``, additionally run the
            ``AdversarialArchitectureCritic`` against the first-pass synthesis and
            return ``{"first_pass", "critic", "verdict"}`` instead of the first-pass
            output alone. Defaults to ``False`` (today's unchanged behavior).
    """
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
        return await _run_architecture_review_chain(
            repo=repo, target_mode=target_mode, diff=diff, llm_provider=llm_provider,
            chain_adversarial=chain_adversarial, provider=provider, model=model,
            temperature=temperature, max_tokens=max_tokens, num_ctx=num_ctx,
            num_predict=num_predict, host=host,
        )
    except Exception as e:
        logging.exception("architecture_review failed")
        raise RuntimeError(str(e)) from e


# ---------------------------------------------------------------------------
# MCP tool: bootstrap_architecture
# ---------------------------------------------------------------------------


@mcp.tool()
async def bootstrap_architecture(
    repo: str,
    task: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Generate an ARCHITECTURE.md document via four-phase codebase analysis.

    Runs reconnaissance, flow trace, abstraction analysis, and synthesis against
    the target GitHub repository, then renders the results as structured markdown.
    Returns the document plus the synthesis findings list.

    Args:
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        task: Optional analysis focus. Default: ``"Bootstrap ARCHITECTURE.md for <repo>"``.
        provider: Optional LLM provider override (``ollama``, ``gemini``, ``openrouter``).
        model: Override the model name.
        temperature: Override temperature.
        max_tokens: Override max output tokens.
        num_ctx: Override context window (Ollama only).
        num_predict: Override num_predict (Ollama only).
        host: Override Ollama host URL.
    """
    import uuid
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState

    resolved_provider = (
        provider
        or _CONFIG.get("llm_provider")
        or os.environ.get("LLM_PROVIDER", "ollama")
    ).lower()
    llm = MonitoredLLMProvider(
        _build_llm_provider(
            resolved_provider,
            host=host,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            num_predict=num_predict,
        ),
        agent_role="architect",
    )
    gateway = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://mcpjungle:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", ""),
    )
    agent = ArchitectAgent(gateway=gateway, llm_provider=llm, repo=repo)
    state: AgentState = {
        "task": task or f"Bootstrap ARCHITECTURE.md for {repo}",
        "task_type": "bootstrap",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)
    if result.get("error"):
        raise RuntimeError(result["error"].get("reason", "bootstrap failed"))
    output = result.get("agent_output") or {}
    return {
        "architecture_md": output.get("architecture_md", ""),
        "summary": output.get("summary", ""),
        "findings": output.get("findings", []),
        "recommendations": output.get("recommendations", []),
    }


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
        chain_adversarial (bool, optional): when true, additionally run the
            AdversarialArchitectureCritic against the first-pass synthesis and
            return {"first_pass", "critic", "verdict"} instead of the first-pass
            output alone. Defaults to false (today's unchanged behavior).

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
        result = await _run_architecture_review_chain(
            repo=repo, target_mode=target_mode, diff=body.get("diff"), llm_provider=llm_provider,
            chain_adversarial=bool(body.get("chain_adversarial")), provider=body.get("provider"),
            model=body.get("model"), temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"), num_ctx=body.get("num_ctx"),
            num_predict=body.get("num_predict"), host=body.get("host"),
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logging.exception("architecture_review failed")
        return JSONResponse({"error": "architecture review failed — see server logs"}, status_code=500)


# ---------------------------------------------------------------------------
# MCP tool: adversarial_architecture_review — attacks the first-pass
# ArchitectAgent synthesis output
# ---------------------------------------------------------------------------


async def _run_adversarial_architecture_review(
    repo: str,
    first_pass_output: dict,
    task: str,
    provider: str | None = None,
    diff: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Run the AdversarialArchitectureCritic and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    gateway = GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id="adversarial-architecture-critic",
        client_secret=os.environ.get("ADVERSARIAL_ARCHITECTURE_CRITIC_SECRET", ""),
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
    llm_provider = MonitoredLLMProvider(llm_provider, agent_role="adversarial_architecture_critic")
    agent = AdversarialArchitectureCritic(gateway=gateway, llm_provider=llm_provider, repo=repo)
    state = AgentState(
        task=task,
        diff=diff or "",
        first_pass_output=first_pass_output,
        thread_id="mcp-call",
        agent_output=None,
        requires_human_approval=False,
        error=None,
    )
    result = await agent.run(state)
    if result.get("error"):
        raise ValueError(result["error"]["reason"])
    return result["agent_output"]


@mcp.tool()
async def adversarial_architecture_review(
    repo: str,
    first_pass_output: dict,
    target_mode: str = "codebase",
    diff: str | None = None,
    provider: str | None = None,
    task: str = _DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
) -> dict:
    """Attack a first-pass architecture_review/ArchitectAgent synthesis output and
    return confirm/refute/escalate findings.

    Args:
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        first_pass_output: The structured synthesis output of a prior architecture review.
        target_mode: ``"codebase"`` (default) or ``"diff"`` — mirrors ``architecture_review``'s
            target shape; informational when combined with ``diff``.
        diff: Unified diff text under review, when the first pass reviewed a diff rather than
            the whole codebase. Included in the critic's grounding context when present.
        provider: Optional LLM provider override (``"ollama"``, ``"gemini"``, or ``"openrouter"``).
        task: High-level instruction passed to the critic.
        model: Override the model name for the resolved provider.
        temperature: Override the temperature setting.
        max_tokens: Override the max tokens / max output tokens / num_predict setting.
        num_ctx: Override the context window (Ollama only).
        num_predict: Override num_predict (Ollama only).
        host: Override the Ollama host URL.
    """
    try:
        return await _run_adversarial_architecture_review(
            repo, first_pass_output, task, provider, diff=diff,
            model=model, temperature=temperature, max_tokens=max_tokens,
            num_ctx=num_ctx, num_predict=num_predict, host=host,
        )
    except Exception as e:
        logging.exception("adversarial_architecture_review failed")
        raise RuntimeError(str(e)) from e


async def _parse_adversarial_architecture_review_body(request: Request) -> tuple[dict, JSONResponse | None]:
    """Parse and validate the POST /review-architecture-adversarial body.

    Returns (body, None) on success or ({}, error_response) on the first failure.
    """
    try:
        body = await request.json()
    except Exception:
        return {}, JSONResponse({"error": "invalid JSON body"}, status_code=422)
    if not body.get("repo"):
        return {}, JSONResponse({"error": "repo is required"}, status_code=422)
    if body.get("first_pass_output") is None:
        return {}, JSONResponse({"error": "first_pass_output is required"}, status_code=422)
    return body, None


@mcp.custom_route("/review-architecture-adversarial", methods=["POST"])
async def http_adversarial_architecture_review(request: Request) -> JSONResponse:
    """Plain HTTP endpoint for the adversarial architecture critic.

    Body (JSON):
        repo               (str, required): GitHub URL that was reviewed
        first_pass_output  (dict, required): structured synthesis output of a prior architecture review
        target_mode         (str, optional): ``"codebase"`` (default) or ``"diff"`` — mirrors /review-architecture's target shape
        diff                (str, optional): unified diff text, when target_mode="diff"
        task                (str, optional): critique instruction
        provider, model, temperature, max_tokens, num_ctx, num_predict, host: same as POST /review-architecture

    Auth: set REVIEW_API_KEY in env (same as POST /review).

    Returns the AdversarialArchitectureCritic's structured findings.
    """
    if not _check_api_key(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body, error = await _parse_adversarial_architecture_review_body(request)
    if error:
        return error

    task = body.get("task", _DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK)
    provider = body.get("provider")

    try:
        findings = await _run_adversarial_architecture_review(
            body["repo"], body["first_pass_output"], task, provider,
            diff=body.get("diff"),
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
        logging.exception("adversarial architecture review failed")
        return JSONResponse({"error": "adversarial architecture review failed — see server logs"}, status_code=500)


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
