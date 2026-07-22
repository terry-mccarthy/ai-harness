import asyncio
import pytest
import jsonschema
from harness_agents.types import REVIEWER_OUTPUT_SCHEMA, ADVERSARIAL_CODE_CRITIC_SCHEMA

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
# chain_adversarial=True (issue #03): review_diff chains the adversarial code
# critic onto the first-pass output and returns a combined verdict.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def chained_review_result(module_gateway_client):
    """review_diff called with chain_adversarial=True, sharing the module-scoped
    gateway client used by the unchained fixture above. Skips on a transient
    provider error rather than failing the whole module."""
    for attempt in range(3):
        result = await module_gateway_client.call_tool(
            "review_diff", {"diff_text": SAMPLE_DIFF, "chain_adversarial": True},
        )
        if isinstance(result, dict):
            return result
        if attempt < 2:
            await asyncio.sleep(10 * (attempt + 1))
    pytest.skip(f"review_diff (chained) returned a provider error after 3 attempts: {str(result)[:120]}")


@pytest.mark.integration
async def test_review_diff_chain_adversarial_returns_combined_shape(chained_review_result):
    """chain_adversarial=True returns {"first_pass", "critic", "verdict"} — the
    first-pass reviewer output, the adversarial critic output, and a synthesized
    overall verdict — instead of the plain first-pass findings."""
    assert set(chained_review_result.keys()) == {"first_pass", "critic", "verdict"}
    jsonschema.validate(chained_review_result["first_pass"], REVIEWER_OUTPUT_SCHEMA)
    jsonschema.validate(chained_review_result["critic"], ADVERSARIAL_CODE_CRITIC_SCHEMA)
    assert chained_review_result["verdict"] in ("pass", "fail")


@pytest.mark.integration
async def test_review_diff_chain_adversarial_false_matches_unchained_shape(
    review_result, module_gateway_client,
):
    """chain_adversarial=False (explicit) returns the same plain response shape
    as the unchained call — this must not be a breaking change for existing
    callers, so the default and the explicit-false shapes must match exactly."""
    result = await module_gateway_client.call_tool(
        "review_diff", {"diff_text": SAMPLE_DIFF, "chain_adversarial": False},
    )
    if not isinstance(result, dict):
        pytest.skip(f"review_diff returned a provider error: {str(result)[:120]}")
    assert set(result.keys()) == set(review_result.keys()) == {"verdict", "findings", "summary"}
    jsonschema.validate(result, REVIEWER_OUTPUT_SCHEMA)
