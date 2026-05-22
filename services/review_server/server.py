import logging
import os
import uvicorn
from mcp.server.fastmcp import FastMCP

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
from mcp.server.transport_security import TransportSecuritySettings

from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState

mcp = FastMCP(
    "review_server",
    host="0.0.0.0",
    port=9003,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
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
    """Factory: instantiate the correct LLMProvider strategy from a provider name.

    Args:
        provider_name: ``"ollama"`` or ``"gemini"``.  Any other value falls
            back to Ollama.

    Returns:
        A concrete :class:`~harness_agents.llm.LLMProvider` instance configured
        from the relevant environment variables.
    """
    from harness_agents.llm import OllamaProvider, GeminiProvider

    if provider_name == "gemini":
        return GeminiProvider(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            api_key=os.environ.get("GEMINI_API_KEY"),
            temperature=_env_float("LLM_TEMPERATURE", 0.1),
            max_output_tokens=_env_int("LLM_MAX_TOKENS", 1024),
        )
    return OllamaProvider(
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=_env_int("OLLAMA_NUM_CTX", 8192),
        temperature=_env_float("LLM_TEMPERATURE", _env_float("OLLAMA_TEMPERATURE", 0.1)),
        num_predict=_env_int("LLM_MAX_TOKENS", _env_int("OLLAMA_NUM_PREDICT", 1024)),
    )


@mcp.tool()
async def review_diff(
    diff_text: str,
    provider: str | None = None,
    task: str = (
        "Review this diff for: "
        "(1) security vulnerabilities — credential leaks, injection flaws, path traversal, missing auth enforcement, insecure defaults; "
        "(2) code quality — error handling gaps, dead code, resource leaks, incorrect types, silent failures; "
        "(3) architectural concerns — hardcoded values, tight coupling, shared mutable state, missing abstractions. "
        "Report every finding with file, line, severity (CRITICAL/WARNING/INFO), and a specific fix suggestion. "
        "Verdict is 'fail' if any CRITICAL finding exists."
    ),
) -> dict:
    """Run the governed code-reviewer agent and return structured findings.

    Args:
        diff_text: The unified diff string to review.
        provider: Optional LLM provider override for this request.  Accepted
            values are ``"ollama"`` and ``"gemini"``.  When omitted the server
            falls back to the ``LLM_PROVIDER`` environment variable (default:
            ``"ollama"``).
        task: High-level review instruction passed to the agent.
    """
    gateway = GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        client_id="code-reviewer",
        client_secret=os.environ.get("CODE_REVIEWER_SECRET", ""),
    )

    # Resolve provider: per-call arg > env var > default
    resolved_provider = (provider or os.environ.get("LLM_PROVIDER", "ollama")).lower()
    llm_provider = _build_llm_provider(resolved_provider)

    agent = CodeReviewerAgent(
        gateway=gateway,
        llm_provider=llm_provider,
    )
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


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9003)
