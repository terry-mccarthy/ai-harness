"""Architecture-review/bootstrap/gate-check orchestration for review_server.

Carved out of server.py (issue #08): the invariant-check/gate-failure glue
behind the ``architecture_review``, ``bootstrap_architecture``,
``adversarial_architecture_review``, and ``execute_architecture_check`` MCP
tools (and the ``/review-architecture``, ``/review-architecture-adversarial``
HTTP routes) — the constant + chaining glue for ``architecture_review``, the
``AdversarialArchitectureCritic`` invocation, the adversarial-HTTP body
parser, the ``bootstrap_architecture`` core (ArchitectAgent invocation +
result shaping), and the ``execute_architecture_check`` core (thin call into
``architecture_gate.runner``). The tool/route handlers themselves stay in
server.py as thin wrappers, exactly like issue #06 left ``GET``/``PUT
/config`` in server.py calling into ``core/config.py``, and issue #07 left
``review_diff``/``adversarial_review`` calling into ``services/code_analysis.py``.

NOTE on this NEW module vs. the pre-existing top-level ``architecture_gate/``
package: this file lives at ``services/review_server/services/architecture_gate.py``
(import path ``services.architecture_gate``) and is unrelated to the
pre-existing top-level ``services/review_server/architecture_gate/`` package
(import path ``architecture_gate``, the ``checkers/``/``runner.py`` static-analysis
gate implementation used by ``execute_architecture_check``). Same basename,
different modules — do not conflate the two, and do not restructure the
top-level package here.

Dependency-injection gotcha (same as issue #07's services/code_analysis.py):
server.py's tests patch ``review_server.GatewayClient``,
``review_server._build_llm_provider``, and (for the chain_adversarial path)
``review_server._run_adversarial_architecture_review`` via
``unittest.mock.patch.object`` on the *server* module. A plain function moved
into this module would otherwise resolve those bare names through *this*
module's own globals at call time — patching server.py's copy would silently
have no effect. To keep that seam working, every function here that needs a
gateway client or an LLM provider accepts optional
``gateway_client_cls``/``build_llm_provider`` overrides (defaulting to this
module's own real implementations), and ``_run_architecture_review_chain``
additionally accepts a ``run_adversarial_architecture_review`` override so the
chain calls whatever server.py's *current* (possibly patched) attribute value
is, rather than silently falling back to this module's own unpatched
original. server.py's thin wrappers always pass their own current values
through explicitly.
"""
import logging
import os

from harness_gateway.client import GatewayClient
from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic
from harness_agents.types import AgentState
from metrics import MonitoredLLMProvider
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.config import _CONFIG
from services.code_analysis import _build_llm_provider, _chain_adversarial_verdict


_DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK = (
    "Attack the first-pass architect's synthesis findings. Confirm, refute, escalate, "
    "downgrade, or leave unresolved each one. A confirmed or escalated HIGH/CRITICAL "
    "finding requires a concrete regression_scenario — a specific failure trace grounded "
    "in the codebase, not a restatement of the severity."
)

_ARCHITECTURE_CHAIN_FAIL_SEVERITIES = {"CRITICAL", "HIGH"}


def _resolve_deps(gateway_client_cls, build_llm_provider):
    """Fall back to this module's real GatewayClient/_build_llm_provider when the
    caller (server.py's thin wrappers) doesn't inject an override — see the
    module docstring's "Dependency-injection gotcha" note.
    """
    return gateway_client_cls or GatewayClient, build_llm_provider or _build_llm_provider


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
    *,
    gateway_client_cls=None,
    build_llm_provider=None,
) -> dict:
    """Run the AdversarialArchitectureCritic and return structured findings.

    Raises ValueError if the agent returns an error.
    """
    gateway_cls, builder = _resolve_deps(gateway_client_cls, build_llm_provider)

    gateway = gateway_cls(
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
    llm_provider = builder(
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
    run_adversarial_architecture_review=None,
) -> dict:
    """Run the first-pass architecture synthesis and, when requested, chain the
    adversarial architecture critic on top of it.

    Returns the first-pass output unchanged when ``chain_adversarial`` is False.
    Otherwise returns ``{"first_pass", "critic", "verdict"}`` where verdict is
    computed per the confirmed/escalated-only-fails rule (HIGH+ threshold).

    ``run_adversarial_architecture_review`` lets server.py thread its own
    (possibly patched) ``_run_adversarial_architecture_review`` attribute
    through — see the module docstring's "Dependency-injection gotcha" note.
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

    run_adversarial = run_adversarial_architecture_review or _run_adversarial_architecture_review
    critic_output = await run_adversarial(
        repo, first_pass_output, _DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK, provider,
        diff=diff, model=model, temperature=temperature, max_tokens=max_tokens,
        num_ctx=num_ctx, num_predict=num_predict, host=host,
    )
    verdict = _chain_adversarial_verdict(
        critic_output.get("findings", []), _ARCHITECTURE_CHAIN_FAIL_SEVERITIES
    )
    return {"first_pass": first_pass_output, "critic": critic_output, "verdict": verdict}


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


async def _run_bootstrap_architecture(
    repo: str,
    task: str | None,
    llm_provider,
    *,
    gateway_client_cls=None,
) -> dict:
    """Run the ArchitectAgent's four-phase bootstrap analysis and shape the result.

    Raises RuntimeError if the agent returns an error.
    """
    import uuid
    from harness_agents.architect import ArchitectAgent

    gateway_cls = gateway_client_cls or GatewayClient
    gateway = gateway_cls(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://mcpjungle:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", ""),
    )
    agent = ArchitectAgent(gateway=gateway, llm_provider=llm_provider, repo=repo)
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


async def _run_execute_architecture_check(target_language: str, repo_path: str) -> dict:
    """Run the static-analysis architecture gate and return a GateSignalContract dict."""
    from architecture_gate.runner import run_gate

    logging.info(
        "execute_architecture_check called for lang=%s repo=%s",
        target_language,
        repo_path,
    )
    signal = await run_gate(repo_path, target_language)
    return signal.to_dict()
