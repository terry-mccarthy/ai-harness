"""Unit tests for AdversarialCodeCritic — mocked gateway/LLM, no live stack."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio

_VALID_CRITIC_OUTPUT = json.dumps({
    "findings": [
        {
            "outcome": "confirmed",
            "severity": "CRITICAL",
            "file": "app/db.py",
            "line": 12,
            "message": "SQL built via f-string interpolation",
            "exploit_scenario": "username=\"' OR '1'='1\" bypasses the WHERE clause and returns all rows",
        }
    ],
    "summary": "Confirmed the SQL injection with a working exploit.",
})

_FIRST_PASS_OUTPUT = {
    "verdict": "fail",
    "findings": [
        {
            "severity": "CRITICAL",
            "file": "app/db.py",
            "line": 12,
            "message": "possible SQL injection",
            "suggestion": "use parameterized queries",
        }
    ],
    "summary": "Found a possible SQL injection.",
}

_SAMPLE_DIFF = (
    "diff --git a/app/db.py b/app/db.py\n"
    "--- a/app/db.py\n+++ b/app/db.py\n"
    "@@ -8,3 +8,3 @@\n"
    "-    cursor.execute(\"SELECT * FROM users WHERE username = ?\", (username,))\n"
    "+    cursor.execute(f\"SELECT * FROM users WHERE username = '{username}'\")\n"
)


def _mock_gateway():
    gw = MagicMock()
    gw.call_tool = AsyncMock(side_effect=lambda name, params: (
        {"diff": _SAMPLE_DIFF} if name == "git_diff" else {"findings": []}
    ))
    return gw


class _MockLLM:
    def __init__(self, content: str = _VALID_CRITIC_OUTPUT):
        self._content = content

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._content)


def _state(**overrides) -> dict:
    base = {
        "task": "Attack the first-pass findings",
        "diff": _SAMPLE_DIFF,
        "first_pass_output": _FIRST_PASS_OUTPUT,
        "thread_id": "t1",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }
    base.update(overrides)
    return base


async def test_critic_returns_structured_output_matching_schema():
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic
    import jsonschema
    from harness_agents.types import ADVERSARIAL_CODE_CRITIC_SCHEMA

    agent = AdversarialCodeCritic(gateway=_mock_gateway(), llm_provider=_MockLLM())
    result = await agent.run(_state())

    assert result["error"] is None
    jsonschema.validate(result["agent_output"], ADVERSARIAL_CODE_CRITIC_SCHEMA)


async def test_critic_confirms_finding_with_exploit_scenario():
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic

    agent = AdversarialCodeCritic(gateway=_mock_gateway(), llm_provider=_MockLLM())
    result = await agent.run(_state())

    findings = result["agent_output"]["findings"]
    assert findings[0]["outcome"] == "confirmed"
    assert findings[0]["exploit_scenario"]


async def test_critic_passes_first_pass_output_to_llm_prompt():
    """The critic's user message includes the first-pass output, not just the raw diff."""
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic

    captured = {}

    class _CapturingLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            captured["messages"] = messages
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    agent = AdversarialCodeCritic(gateway=_mock_gateway(), llm_provider=_CapturingLLM())
    await agent.run(_state())

    user_content = captured["messages"][-1]["content"]
    assert "possible SQL injection" in user_content


async def test_critic_reuses_gathered_tool_results_not_raw_diff_only():
    """The critic calls git_diff/run_linter itself (reusing gathered context), same as the reviewer."""
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic

    gw = _mock_gateway()
    agent = AdversarialCodeCritic(gateway=gw, llm_provider=_MockLLM())
    await agent.run(_state())

    called_tools = {call.args[0] for call in gw.call_tool.await_args_list}
    assert called_tools == {"git_diff", "run_linter"}


async def test_critic_retries_on_invalid_output_then_succeeds():
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic

    class _FlakyLLM:
        def __init__(self):
            self._calls = 0

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            self._calls += 1
            if self._calls == 1:
                return LLMResponse(content="not json")
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    agent = AdversarialCodeCritic(gateway=_mock_gateway(), llm_provider=_FlakyLLM())
    result = await agent.run(_state())

    assert result["error"] is None
    assert result["agent_output"]["findings"][0]["outcome"] == "confirmed"


async def test_critic_gives_up_after_max_iterations_invalid_output():
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic

    agent = AdversarialCodeCritic(gateway=_mock_gateway(), llm_provider=_MockLLM(content="not json, ever"))
    result = await agent.run(_state())

    assert result["agent_output"] is None
    assert result["error"]["code"] == "invalid_output"


async def test_critic_denies_gracefully_on_tool_access_denied():
    from harness_agents.adversarial_code_critic import AdversarialCodeCritic
    from harness_gateway.client import ToolAccessDenied

    gw = MagicMock()
    gw.call_tool = AsyncMock(side_effect=ToolAccessDenied("403 Forbidden: git_diff"))
    agent = AdversarialCodeCritic(gateway=gw, llm_provider=_MockLLM())

    result = await agent.run(_state())

    assert result["error"]["code"] == "tool_access_denied"
