from contextlib import asynccontextmanager
import logging
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from harness_gateway.client import GatewayClient
from metrics import MonitoredLLMProvider
from core.config import (
    _CONFIG,
    _check_api_key,
    _close_pg_pool,
    _ensure_config_table,
    _get_cfg,
    _init_pg_pool,
    _load_config_from_pg,
    _pg_pool_connected,
)
from routers.config import register_config_routes
from services.code_analysis import (
    _DEFAULT_ADVERSARIAL_TASK,
    _build_llm_provider,
    _chain_adversarial_verdict,
    _run_adversarial_review as _ca_run_adversarial_review,
    _run_review_maybe_chained as _ca_run_review_maybe_chained,
)
from services.architecture_gate import (
    _DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK,
    _parse_adversarial_architecture_review_body as _ag_parse_adversarial_architecture_review_body,
    _run_adversarial_architecture_review as _ag_run_adversarial_architecture_review,
    _run_architecture_review_chain as _ag_run_architecture_review_chain,
    _run_bootstrap_architecture as _ag_run_bootstrap_architecture,
    _run_execute_architecture_check as _ag_run_execute_architecture_check,
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
#
# The GET/PUT /config route handlers now live in routers/config.py (issue
# #09), built on the config infra core.config.py already extracted (issue
# #06). There's no GatewayClient/LLM-provider construction behind these
# routes, so no DI seam/thin-wrapper is needed here — register_config_routes
# just needs the live `mcp` instance to decorate its routes onto, since
# `mcp.custom_route` (unlike FastAPI's APIRouter) has no standalone
# router object to build and mount separately.
# ---------------------------------------------------------------------------

register_config_routes(mcp)


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
#
# The chain constant/glue (_ARCHITECTURE_CHAIN_FAIL_SEVERITIES,
# _run_architecture_review_chain) now lives in services/architecture_gate.py
# (issue #08). This thin wrapper exists so that unittest.mock.patch.object(
# review_server, "_run_adversarial_architecture_review", ...) (used in
# packages/harness-tests/test_review_mcp.py and test_review_http.py) keeps
# affecting the chain_adversarial=True path: that name is resolved as a bare
# global *inside this module* at call time, so this wrapper reads its own
# (possibly patched) current value and threads it into
# services.architecture_gate explicitly, rather than letting the moved chain
# function silently fall back to its own module's unpatched original.
# ---------------------------------------------------------------------------


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
    return await _ag_run_architecture_review_chain(
        repo=repo, target_mode=target_mode, diff=diff, llm_provider=llm_provider,
        chain_adversarial=chain_adversarial, provider=provider, model=model,
        temperature=temperature, max_tokens=max_tokens, num_ctx=num_ctx,
        num_predict=num_predict, host=host,
        run_adversarial_architecture_review=_run_adversarial_architecture_review,
    )


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
#
# The ArchitectAgent invocation/state-building/result-shaping now lives in
# services/architecture_gate.py (issue #08) as _run_bootstrap_architecture.
# The LLM-provider build + MonitoredLLMProvider wrap stay here (mirroring
# architecture_review's wrapper) so that unittest.mock.patch.object(
# review_server, "_build_llm_provider" / "MonitoredLLMProvider" / "GatewayClient",
# ...) (used in packages/harness-tests/test_unit_bootstrap_mcp_tool.py) keeps
# affecting this tool: those names are resolved as bare globals *inside this
# module* at call time.
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
    return await _ag_run_bootstrap_architecture(repo, task, llm, gateway_client_cls=GatewayClient)


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
#
# _run_adversarial_architecture_review's real implementation (GatewayClient +
# LLM-provider construction, AdversarialArchitectureCritic invocation) now
# lives in services/architecture_gate.py (issue #08). This thin wrapper exists
# so that (a) tests patching review_server.GatewayClient /
# review_server._build_llm_provider still take effect (see the
# _run_architecture_review_chain wrapper above for why), and (b)
# packages/harness-tests/test_adversarial_architecture_review_http.py, which
# calls review_server._run_adversarial_architecture_review(...) directly
# (bypassing the adversarial_architecture_review tool entirely), keeps working
# unchanged.
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
    return await _ag_run_adversarial_architecture_review(
        repo, first_pass_output, task, provider, diff=diff,
        model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
        gateway_client_cls=GatewayClient, build_llm_provider=_build_llm_provider,
    )


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


# _parse_adversarial_architecture_review_body's implementation now lives in
# services/architecture_gate.py (issue #08) — it's a pure request-body
# validation helper with no GatewayClient/LLM dependency, so no DI seam is
# needed; it's imported directly as _ag_parse_adversarial_architecture_review_body.


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

    body, error = await _ag_parse_adversarial_architecture_review_body(request)
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
# MCP tool: execute_architecture_check
#
# The run_gate invocation now lives in services/architecture_gate.py (issue
# #08) as _run_execute_architecture_check. No DI seam is needed here — no
# test patches review_server.run_gate or the architecture_gate.runner module.
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
    return await _ag_run_execute_architecture_check(target_language, repo_path)


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
