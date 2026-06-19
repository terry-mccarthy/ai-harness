import logging
import os
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState

mcp = FastMCP(
    "review_server",
    host="0.0.0.0",
    port=9003,
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


def _build_llm_provider(provider_name: str):
    """Factory: return a concrete LLMProvider for the given provider name."""
    from harness_agents.llm import OllamaProvider, GeminiProvider, OpenRouterProvider

    if provider_name == "gemini":
        return GeminiProvider(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            api_key=os.environ.get("GEMINI_API_KEY"),
            temperature=_env_float("LLM_TEMPERATURE", 0.1),
            max_output_tokens=_env_int("LLM_MAX_TOKENS", 1024),
        )
    if provider_name == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY env var is required for the openrouter provider")
        return OpenRouterProvider(
            api_key=api_key,
            model=os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
            temperature=_env_float("LLM_TEMPERATURE", 0.1),
            max_tokens=_env_int("LLM_MAX_TOKENS", 1024),
        )
    if provider_name == "ollama":
        return OllamaProvider(
            host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
            num_ctx=_env_int("OLLAMA_NUM_CTX", 8192),
            temperature=_env_float("LLM_TEMPERATURE", _env_float("OLLAMA_TEMPERATURE", 0.1)),
            num_predict=_env_int("LLM_MAX_TOKENS", _env_int("OLLAMA_NUM_PREDICT", 1024)),
        )
    raise ValueError(
        f"Unknown LLM provider: {provider_name!r}. Supported: ollama, gemini, openrouter"
    )


async def _run_review(diff_text: str, task: str, provider: str | None) -> dict:
    """Run the CodeReviewerAgent and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    gateway = GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id="code-reviewer",
        client_secret=os.environ.get("CODE_REVIEWER_SECRET", ""),
    )
    resolved_provider = (provider or os.environ.get("LLM_PROVIDER", "ollama")).lower()
    llm_provider = _build_llm_provider(resolved_provider)
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


@mcp.tool()
async def review_diff(
    diff_text: str,
    provider: str | None = None,
    task: str = _DEFAULT_TASK,
) -> dict:
    """Run the governed code-reviewer agent and return structured findings.

    Args:
        diff_text: The unified diff string to review.
        provider: Optional LLM provider override (``"ollama"``, ``"gemini"``, or ``"openrouter"``).
            Falls back to the ``LLM_PROVIDER`` environment variable.
        task: High-level review instruction passed to the agent.
    """
    try:
        return await _run_review(diff_text, task, provider)
    except Exception as e:
        logging.exception("review_diff failed")
        raise RuntimeError(str(e)) from e


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
        diff_text (str, required): unified diff to review
        task      (str, optional): review instruction
        provider  (str, optional): ``"ollama"``, ``"gemini"``, or ``"openrouter"``

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
        findings = await _run_review(diff_text, task, provider)
        return JSONResponse(findings)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logging.exception("review failed")
        return JSONResponse({"error": "review failed — see server logs"}, status_code=500)


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


@mcp.tool()
async def architecture_review(
    target_mode: str,
    repo: str,
    diff: str | None = None,
    provider: str | None = None,
) -> dict:
    """Score a codebase or diff against the repo's stated architectural invariants.

    Fetches ``ARCHITECTURE.md`` and ADRs from the GitHub repo via the GitHub API,
    then scores the codebase file tree (``target_mode="codebase"``) or a unified
    diff (``target_mode="diff"``) against the stated invariants.

    Args:
        target_mode: ``"codebase"`` (scan file tree) or ``"diff"`` (score a unified diff).
        repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        diff: Unified diff text (required when ``target_mode="diff"``).
        provider: Optional LLM provider override (``"ollama"``, ``"gemini"``, or ``"openrouter"``).
            Falls back to the ``LLM_PROVIDER`` environment variable.
    """
    from architecture_review import architecture_review as _architecture_review

    resolved_provider = (provider or os.environ.get("LLM_PROVIDER", "ollama")).lower()
    llm_provider = _build_llm_provider(resolved_provider)
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


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9003)
