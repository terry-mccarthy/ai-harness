import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import jsonschema
from harness_agents.types import REVIEWER_OUTPUT_SCHEMA

SAMPLE_DIFF = """
diff --git a/auth.py b/auth.py
index 1a2b3c4..5d6e7f8 100644
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,8 @@ def login(username, password):
     user = db.find(username)
+    print(f"Login attempt: {username} {password}")   # obvious secret leak
     if user and user.check_password(password):
         return generate_token(user)
"""


@pytest.fixture(scope="module")
async def review_result(module_gateway_client):
    """Single LLM call shared by all three tests to avoid back-to-back rate limits.

    Skips the module if the provider returns a transient error (503 rate limit,
    provider_error, etc.) so a flaky upstream doesn't block the suite.
    """
    for attempt in range(3):
        result = await module_gateway_client.call_tool("review_diff", {"diff_text": SAMPLE_DIFF})
        if isinstance(result, dict):
            return result
        if attempt < 2:
            await asyncio.sleep(10 * (attempt + 1))
    pytest.skip(f"review_diff returned a provider error after 3 attempts: {str(result)[:120]}")


@pytest.mark.integration
async def test_review_diff_tool_is_reachable(review_result):
    """review_diff is registered in MCPJungle and callable."""
    assert review_result is not None


@pytest.mark.integration
async def test_review_diff_returns_valid_schema(review_result):
    """review_diff output satisfies REVIEWER_OUTPUT_SCHEMA."""
    jsonschema.validate(review_result, REVIEWER_OUTPUT_SCHEMA)


@pytest.mark.integration
async def test_review_diff_catches_credential_leak(review_result):
    """review_diff detects the password logging vulnerability and fails the diff."""
    assert review_result["verdict"] == "fail"
    assert any(f["severity"] == "CRITICAL" for f in review_result["findings"])


# ---------------------------------------------------------------------------
# architecture_review MCP tool — chain_adversarial (issue #04)
#
# No Docker/live provider needed here: GatewayClient and the LLM provider are
# mocked in-process, mirroring test_adversarial_architecture_review_http.py's
# approach, so these run alongside the rest of the (mocked) unit suite rather
# than requiring the live integration stack the tests above need.
# ---------------------------------------------------------------------------

_REVIEW_SERVER_PATH = Path(__file__).resolve().parents[2] / "services" / "review_server" / "server.py"
_REVIEW_SERVER_MODULE = "_review_server_under_test"


def _load_review_server():
    if _REVIEW_SERVER_MODULE in sys.modules:
        return sys.modules[_REVIEW_SERVER_MODULE]
    rs_dir = str(_REVIEW_SERVER_PATH.parent)
    if rs_dir not in sys.path:
        sys.path.insert(0, rs_dir)
    spec = importlib.util.spec_from_file_location(_REVIEW_SERVER_MODULE, _REVIEW_SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_REVIEW_SERVER_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


_ARCH_FIRST_PASS = {"compliant": True, "findings": [], "summary": "ok"}

_ARCH_CRITIC_CONFIRMED_HIGH = {
    "findings": [
        {
            "outcome": "confirmed",
            "severity": "HIGH",
            "location": "shopflow/routes.py",
            "message": "business logic inline in the route handler",
            "regression_scenario": "adding a second payment provider requires editing every route handler",
        }
    ],
    "summary": "Confirmed with a concrete regression trace.",
}

_ARCH_CRITIC_REFUTED_HIGH = {
    "findings": [
        {
            "outcome": "refuted",
            "severity": "HIGH",
            "location": "shopflow/routes.py",
            "message": "not actually a regression — this path is covered by an existing abstraction",
        }
    ],
    "summary": "Refuted — no regression.",
}


def _mcp_env():
    return patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"})


async def test_mcp_architecture_review_chain_adversarial_defaults_false_unchanged():
    """Omitting chain_adversarial returns exactly the first-pass output — unchanged
    from today's behavior (regression/contract test)."""
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    with (
        patch.object(review_server, "_build_llm_provider", return_value=MagicMock()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch("architecture_review.architecture_review", AsyncMock(return_value=_ARCH_FIRST_PASS)),
        _mcp_env(),
    ):
        result = await review_server.architecture_review(
            target_mode="codebase", repo="https://github.com/o/r",
        )
    assert result == _ARCH_FIRST_PASS


async def test_mcp_architecture_review_chain_adversarial_explicit_false_unchanged():
    """Explicit chain_adversarial=False behaves identically to the default."""
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    with (
        patch.object(review_server, "_build_llm_provider", return_value=MagicMock()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch("architecture_review.architecture_review", AsyncMock(return_value=_ARCH_FIRST_PASS)),
        _mcp_env(),
    ):
        result = await review_server.architecture_review(
            target_mode="codebase", repo="https://github.com/o/r", chain_adversarial=False,
        )
    assert result == _ARCH_FIRST_PASS


async def test_mcp_architecture_review_chain_adversarial_true_returns_combined_result():
    """chain_adversarial=True returns first_pass, critic, and a synthesized verdict."""
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    with (
        patch.object(review_server, "_build_llm_provider", return_value=MagicMock()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch("architecture_review.architecture_review", AsyncMock(return_value=_ARCH_FIRST_PASS)),
        patch.object(
            review_server,
            "_run_adversarial_architecture_review",
            AsyncMock(return_value=_ARCH_CRITIC_CONFIRMED_HIGH),
        ),
        _mcp_env(),
    ):
        result = await review_server.architecture_review(
            target_mode="codebase", repo="https://github.com/o/r", chain_adversarial=True,
        )
    assert result["first_pass"] == _ARCH_FIRST_PASS
    assert result["critic"] == _ARCH_CRITIC_CONFIRMED_HIGH
    assert result["verdict"] == "fail"


async def test_mcp_architecture_review_chain_adversarial_refuted_high_does_not_fail():
    """A first-pass HIGH+ finding the critic refutes does not, by itself, count toward
    a failing assessment."""
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    with (
        patch.object(review_server, "_build_llm_provider", return_value=MagicMock()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch("architecture_review.architecture_review", AsyncMock(return_value=_ARCH_FIRST_PASS)),
        patch.object(
            review_server,
            "_run_adversarial_architecture_review",
            AsyncMock(return_value=_ARCH_CRITIC_REFUTED_HIGH),
        ),
        _mcp_env(),
    ):
        result = await review_server.architecture_review(
            target_mode="codebase", repo="https://github.com/o/r", chain_adversarial=True,
        )
    assert result["verdict"] == "pass"
