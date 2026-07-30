"""LLM-provider factory and review-agent orchestration for review_server.

Carved out of server.py (issue #07): the LLM provider factory
(``_build_gemini_provider``/``_build_openrouter_provider``/``_build_ollama_provider``/
``_BUILDERS``/``_build_llm_provider``) and the code-review agent orchestration
functions (``_run_review``, ``_run_review_maybe_chained``,
``_chain_adversarial_verdict``, ``_run_adversarial_review``,
``_run_chained_review``) that back the ``review_diff``/``adversarial_review``
MCP tools and the ``POST /review``/``POST /review-adversarial`` HTTP routes.
The tool/route handlers themselves stay in server.py as thin wrappers, exactly
like issue #06 left ``GET``/``PUT /config`` in server.py calling into
``core/config.py``.

NOTE on this NEW module vs. the OLD top-level ``code_analysis.py``: this file
lives at ``services/review_server/services/code_analysis.py`` (import path
``services.code_analysis``) and is unrelated to the pre-existing top-level
``services/review_server/code_analysis.py`` (import path ``code_analysis``,
GitHub-API-based complexity/hotspot/coupling analysis used by the
``code_health_score``/``codebase_hotspots``/``logical_coupling`` tools). Same
basename, different modules — do not conflate the two.

Dependency-injection gotcha: server.py's tests patch ``review_server.GatewayClient``
and ``review_server._build_llm_provider`` via ``unittest.mock.patch.object`` on the
*server* module. A plain function moved into this module would otherwise resolve
those bare names through *this* module's own globals at call time — patching
server.py's copy would silently have no effect. To keep that seam working,
every function here that needs a gateway client or an LLM provider accepts
optional ``gateway_client_cls``/``build_llm_provider`` overrides (defaulting to
this module's own real implementations) and threads them through to any nested
calls. server.py's thin wrappers always pass its own *current* (possibly
patched) attribute values through explicitly.
"""
import logging
import os

from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.adversarial_code_critic import AdversarialCodeCritic
from harness_agents.types import AgentState
from metrics import MonitoredLLMProvider
from core.config import _CONFIG, _get_cfg


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


def _resolve_deps(gateway_client_cls, build_llm_provider):
    """Fall back to this module's real GatewayClient/_build_llm_provider when the
    caller (server.py's thin wrappers) doesn't inject an override — see the
    module docstring's "Dependency-injection gotcha" note.
    """
    return gateway_client_cls or GatewayClient, build_llm_provider or _build_llm_provider


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
    *,
    gateway_client_cls=None,
    build_llm_provider=None,
) -> dict:
    """Run the CodeReviewerAgent and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    gateway_cls, builder = _resolve_deps(gateway_client_cls, build_llm_provider)

    gateway = gateway_cls(
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
    llm_provider = builder(
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
    gateway_client_cls=None,
    build_llm_provider=None,
) -> dict:
    """Shared branch point for review_diff / POST /review.

    chain_adversarial=False (the default) returns exactly what _run_review
    returns — byte-for-byte identical to today's response. chain_adversarial=True
    returns the combined {"first_pass", "critic", "verdict"} shape from
    _run_chained_review.
    """
    if chain_adversarial:
        return await _run_chained_review(
            diff_text, task, provider,
            model=model, temperature=temperature, max_tokens=max_tokens,
            num_ctx=num_ctx, num_predict=num_predict, host=host,
            gateway_client_cls=gateway_client_cls, build_llm_provider=build_llm_provider,
        )
    return await _run_review(
        diff_text, task, provider,
        model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
        gateway_client_cls=gateway_client_cls, build_llm_provider=build_llm_provider,
    )


def _chain_adversarial_verdict(critic_findings: list[dict], fail_severities: set[str]) -> str:
    """'fail' if any critic finding is at a fail-tier severity with outcome
    confirmed/escalated; 'pass' otherwise. A refuted/downgraded/unresolved
    finding never fails the build on its own.
    """
    for finding in critic_findings:
        if finding["severity"] in fail_severities and finding["outcome"] in ("confirmed", "escalated"):
            return "fail"
    return "pass"


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
    *,
    gateway_client_cls=None,
    build_llm_provider=None,
) -> dict:
    """Run the AdversarialCodeCritic and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    gateway_cls, builder = _resolve_deps(gateway_client_cls, build_llm_provider)

    gateway = gateway_cls(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id="adversarial-code-critic",
        client_secret=os.environ.get("ADVERSARIAL_CODE_CRITIC_SECRET", ""),
    )
    resolved_provider = (
        provider
        or _CONFIG.get("llm_provider")
        or os.environ.get("LLM_PROVIDER", "ollama")
    ).lower()
    llm_provider = builder(
        resolved_provider,
        host=host,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )
    llm_provider = MonitoredLLMProvider(llm_provider, agent_role="adversarial_code_critic")
    agent = AdversarialCodeCritic(gateway=gateway, llm_provider=llm_provider)
    state = AgentState(
        task=task,
        diff=diff_text,
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


_DEFAULT_ADVERSARIAL_TASK = (
    "Attack the first-pass reviewer's findings. Confirm, refute, escalate, downgrade, "
    "or leave unresolved each one. A confirmed or escalated CRITICAL finding requires "
    "a concrete exploit_scenario — an actual input or request that triggers it, not a "
    "restatement of the severity."
)


async def _run_chained_review(
    diff_text: str,
    task: str,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    host: str | None = None,
    *,
    gateway_client_cls=None,
    build_llm_provider=None,
) -> dict:
    """Run the first-pass reviewer, then attack its output with the adversarial
    code critic, and return the combined result.

    Raises ValueError if either stage's agent returns an error.
    """
    first_pass_output = await _run_review(
        diff_text, task, provider,
        model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
        gateway_client_cls=gateway_client_cls, build_llm_provider=build_llm_provider,
    )
    critic_output = await _run_adversarial_review(
        diff_text, first_pass_output, _DEFAULT_ADVERSARIAL_TASK, provider,
        model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
        gateway_client_cls=gateway_client_cls, build_llm_provider=build_llm_provider,
    )
    verdict = _chain_adversarial_verdict(critic_output["findings"], {"CRITICAL"})
    return {"first_pass": first_pass_output, "critic": critic_output, "verdict": verdict}
