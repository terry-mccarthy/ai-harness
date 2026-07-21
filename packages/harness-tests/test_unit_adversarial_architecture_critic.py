"""Unit tests for AdversarialArchitectureCritic — mocked gateway/LLM, no live stack."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio

_VALID_CRITIC_OUTPUT = json.dumps({
    "findings": [
        {
            "outcome": "confirmed",
            "severity": "HIGH",
            "location": "shopflow/routes.py",
            "message": "Business logic lives inline in the HTTP handler, violating ADR-0001.",
            "regression_scenario": "Adding a second payment provider requires editing every route handler instead of one service class; the untested inline charge call already caused a prod double-charge.",
        }
    ],
    "summary": "Confirmed the layering violation with a concrete regression trace.",
})

_FIRST_PASS_OUTPUT = {
    "title": "Architecture Review: shopflow",
    "status": "completed",
    "summary": "Fat controller pattern throughout.",
    "findings": [
        {
            "severity": "MEDIUM",
            "category": "layering",
            "title": "Business logic inline in route handler",
            "message": "pricing/payment/persistence logic all live in create_order()",
            "location": "shopflow/routes.py",
            "phase_origin": "flow_trace",
        }
    ],
    "recommendations": [{"priority": 1, "action": "extract a service layer", "rationale": "testability", "roi": "high"}],
}


def _mock_gateway():
    gw = MagicMock()

    async def _call_tool(name, params):
        if name == "codebase_search":
            return {"results": [{"path": "shopflow/routes.py", "matches": [{"fragment": "def create_order(): ..."}]}]}
        if name == "adr_read":
            return {"adrs": [{"id": "0001", "title": "Layered architecture", "content": "domain logic must be isolated"}]}
        if name == "codebase_hotspots":
            return [{"path": "shopflow/routes.py", "complexity": 184, "rank": 1}]
        return {}

    gw.call_tool = AsyncMock(side_effect=_call_tool)
    return gw


class _MockLLM:
    def __init__(self, content: str = _VALID_CRITIC_OUTPUT):
        self._content = content

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._content)


def _state(**overrides) -> dict:
    base = {
        "task": "Attack the first-pass architecture findings",
        "first_pass_output": _FIRST_PASS_OUTPUT,
        "thread_id": "t1",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }
    base.update(overrides)
    return base


async def test_critic_returns_structured_output_matching_schema():
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic
    import jsonschema
    from harness_agents.types import ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA

    agent = AdversarialArchitectureCritic(gateway=_mock_gateway(), llm_provider=_MockLLM(), repo="https://github.com/example/shopflow")
    result = await agent.run(_state())

    assert result["error"] is None
    jsonschema.validate(result["agent_output"], ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


async def test_critic_confirms_finding_with_regression_scenario():
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic

    agent = AdversarialArchitectureCritic(gateway=_mock_gateway(), llm_provider=_MockLLM(), repo="https://github.com/example/shopflow")
    result = await agent.run(_state())

    findings = result["agent_output"]["findings"]
    assert findings[0]["outcome"] == "confirmed"
    assert findings[0]["regression_scenario"]


async def test_critic_passes_first_pass_output_to_llm_prompt():
    """The critic's user message includes the first-pass synthesis output, not just the target."""
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic

    captured = {}

    class _CapturingLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            captured["messages"] = messages
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    agent = AdversarialArchitectureCritic(gateway=_mock_gateway(), llm_provider=_CapturingLLM(), repo="https://github.com/example/shopflow")
    await agent.run(_state())

    user_content = captured["messages"][-1]["content"]
    assert "Business logic inline in route handler" in user_content


async def test_critic_includes_diff_in_prompt_when_target_is_a_diff():
    """When state['diff'] is set (target_mode='diff'), the diff text is embedded
    in the user message alongside the recon context, not silently dropped."""
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic

    captured = {}

    class _CapturingLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            captured["messages"] = messages
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    diff_text = "diff --git a/shopflow/routes.py b/shopflow/routes.py\n+charge = stripe.Charge.create(...)"
    agent = AdversarialArchitectureCritic(gateway=_mock_gateway(), llm_provider=_CapturingLLM(), repo="https://github.com/example/shopflow")
    await agent.run(_state(diff=diff_text))

    user_content = captured["messages"][-1]["content"]
    assert diff_text in user_content


async def test_critic_reuses_architect_tools_for_grounding_context():
    """The critic calls codebase_search/adr_read/codebase_hotspots itself, same tool surface as the architect."""
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic

    gw = _mock_gateway()
    agent = AdversarialArchitectureCritic(gateway=gw, llm_provider=_MockLLM(), repo="https://github.com/example/shopflow")
    await agent.run(_state())

    called_tools = {call.args[0] for call in gw.call_tool.await_args_list}
    assert called_tools == {"codebase_search", "adr_read", "codebase_hotspots"}


async def test_critic_retries_on_invalid_output_then_succeeds():
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic

    class _FlakyLLM:
        def __init__(self):
            self._calls = 0

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            self._calls += 1
            if self._calls == 1:
                return LLMResponse(content="not json")
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    agent = AdversarialArchitectureCritic(gateway=_mock_gateway(), llm_provider=_FlakyLLM(), repo="https://github.com/example/shopflow")
    result = await agent.run(_state())

    assert result["error"] is None
    assert result["agent_output"]["findings"][0]["outcome"] == "confirmed"


async def test_critic_gives_up_after_max_iterations_invalid_output():
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic

    agent = AdversarialArchitectureCritic(
        gateway=_mock_gateway(), llm_provider=_MockLLM(content="not json, ever"), repo="https://github.com/example/shopflow",
    )
    result = await agent.run(_state())

    assert result["agent_output"] is None
    assert result["error"]["code"] == "invalid_output"


async def test_critic_denies_gracefully_on_tool_access_denied():
    from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic
    from harness_gateway.client import ToolAccessDenied

    gw = MagicMock()
    gw.call_tool = AsyncMock(side_effect=ToolAccessDenied("403 Forbidden: codebase_search"))
    agent = AdversarialArchitectureCritic(gateway=gw, llm_provider=_MockLLM(), repo="https://github.com/example/shopflow")

    result = await agent.run(_state())

    assert result["error"]["code"] == "tool_access_denied"
