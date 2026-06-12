"""Token usage tracking: LLMResponse fields, provider capture, agent accumulation, budget enforcement.

TDD — tests written before implementation.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_REVIEW = json.dumps({
    "verdict": "pass",
    "findings": [],
    "summary": "Looks good.",
})


class _MockLLMWithTokens:
    def __init__(self, content: str, prompt_tokens: int = 30, completion_tokens: int = 15):
        self._content = content
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(
            content=self._content,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
        )


def _mock_gateway():
    gw = MagicMock()

    async def call_tool(name, params=None):
        return {"result": "ok"}

    gw.call_tool = call_tool
    return gw


def _reviewer_state(**overrides) -> dict:
    base = {
        "task": "review diff",
        "diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x=1\n+x=2",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "token_budget": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Slice 1 — LLMResponse carries token counts
# ---------------------------------------------------------------------------

def test_llm_response_has_token_fields():
    from harness_agents.llm import LLMResponse

    r = LLMResponse(content="hello", prompt_tokens=10, completion_tokens=5)
    assert r.prompt_tokens == 10
    assert r.completion_tokens == 5


def test_llm_response_defaults_to_zero():
    from harness_agents.llm import LLMResponse

    r = LLMResponse(content="hello")
    assert r.prompt_tokens == 0
    assert r.completion_tokens == 0


# ---------------------------------------------------------------------------
# Slice 2 — OllamaProvider captures usage from API response
# ---------------------------------------------------------------------------

async def test_ollama_provider_captures_token_counts():
    from harness_agents.llm import OllamaProvider

    mock_resp = MagicMock()
    mock_resp.message.content = "result text"
    mock_resp.prompt_eval_count = 42
    mock_resp.eval_count = 17

    with patch("ollama.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        provider = OllamaProvider(host="http://localhost:11434", model="test-model")
        result = await provider.chat([{"role": "user", "content": "hello"}])

    assert result.content == "result text"
    assert result.prompt_tokens == 42
    assert result.completion_tokens == 17


async def test_ollama_provider_none_counts_become_zero():
    """Ollama returns None for eval counts on cached responses; those must default to 0."""
    from harness_agents.llm import OllamaProvider

    mock_resp = MagicMock()
    mock_resp.message.content = "cached"
    mock_resp.prompt_eval_count = None
    mock_resp.eval_count = None

    with patch("ollama.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        provider = OllamaProvider(host="http://localhost:11434", model="test-model")
        result = await provider.chat([{"role": "user", "content": "hello"}])

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


# ---------------------------------------------------------------------------
# Slice 3 — AgentState type definition includes token fields
# ---------------------------------------------------------------------------

def test_agent_state_accepts_token_fields():
    """AgentState TypedDict must accept token_usage and token_budget keys."""
    from harness_agents.types import AgentState

    state: AgentState = {
        "task": "review",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "token_budget": None,
    }
    assert state["token_usage"]["prompt_tokens"] == 0
    assert state["token_budget"] is None


# ---------------------------------------------------------------------------
# Slice 4 — CodeReviewerAgent accumulates token_usage from LLM responses
# ---------------------------------------------------------------------------

async def test_reviewer_accumulates_token_usage():
    """After a successful review, returned state carries the accumulated token counts."""
    from harness_agents.reviewer import CodeReviewerAgent

    agent = CodeReviewerAgent(
        gateway=_mock_gateway(),
        llm_provider=_MockLLMWithTokens(_VALID_REVIEW, prompt_tokens=30, completion_tokens=15),
    )
    result = await agent.run(_reviewer_state())

    assert result["error"] is None
    usage = result["token_usage"]
    assert usage["prompt_tokens"] == 30
    assert usage["completion_tokens"] == 15


async def test_reviewer_accumulates_across_retries():
    """When the LLM returns invalid JSON the first time, tokens accumulate over retries."""
    from harness_agents.reviewer import CodeReviewerAgent

    call_count = 0
    invalid_then_valid = [
        ("not-json", 20, 10),    # first attempt — invalid
        (_VALID_REVIEW, 25, 12), # second attempt — valid
    ]

    class _SequencedLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            nonlocal call_count
            content, pt, ct = invalid_then_valid[min(call_count, len(invalid_then_valid) - 1)]
            call_count += 1
            return LLMResponse(content=content, prompt_tokens=pt, completion_tokens=ct)

    agent = CodeReviewerAgent(gateway=_mock_gateway(), llm_provider=_SequencedLLM())
    result = await agent.run(_reviewer_state())

    assert result["error"] is None
    assert result["token_usage"]["prompt_tokens"] == 45   # 20 + 25
    assert result["token_usage"]["completion_tokens"] == 22  # 10 + 12


# ---------------------------------------------------------------------------
# Slice 5 — token_budget enforcement: abort retry when budget exceeded
# ---------------------------------------------------------------------------

async def test_reviewer_budget_exceeded_on_retry():
    """After a failed parse attempt whose tokens push usage over budget, return budget error."""
    from harness_agents.reviewer import CodeReviewerAgent

    # Returns invalid JSON so it always fails to parse; each call uses 100 completion tokens
    agent = CodeReviewerAgent(
        gateway=_mock_gateway(),
        llm_provider=_MockLLMWithTokens("not-json", prompt_tokens=50, completion_tokens=100),
    )
    result = await agent.run(_reviewer_state(token_budget=50))

    assert result["error"] is not None
    assert result["error"]["code"] == "token_budget_exceeded"
    # Accumulated usage must be reflected even in the error state
    assert result["token_usage"]["completion_tokens"] >= 100


async def test_reviewer_no_budget_runs_to_completion():
    """When token_budget is None, no budget check fires regardless of tokens used."""
    from harness_agents.reviewer import CodeReviewerAgent

    agent = CodeReviewerAgent(
        gateway=_mock_gateway(),
        llm_provider=_MockLLMWithTokens(_VALID_REVIEW, prompt_tokens=9999, completion_tokens=9999),
    )
    result = await agent.run(_reviewer_state(token_budget=None))

    assert result["error"] is None
    assert result["token_usage"]["completion_tokens"] == 9999
