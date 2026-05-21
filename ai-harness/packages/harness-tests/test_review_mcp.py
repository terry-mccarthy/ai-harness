import pytest
import jsonschema
from harness_agents.types import REVIEWER_OUTPUT_SCHEMA
from harness_gateway.client import GatewayClient

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


@pytest.mark.integration
async def test_review_diff_tool_is_reachable(gateway_client):
    """review_diff is registered in MCPJungle and callable."""
    result = await gateway_client.call_tool("review_diff", {"diff_text": SAMPLE_DIFF})
    assert result is not None


@pytest.mark.integration
async def test_review_diff_returns_valid_schema(gateway_client):
    """review_diff output satisfies REVIEWER_OUTPUT_SCHEMA."""
    result = await gateway_client.call_tool("review_diff", {"diff_text": SAMPLE_DIFF})
    jsonschema.validate(result, REVIEWER_OUTPUT_SCHEMA)


@pytest.mark.integration
async def test_review_diff_catches_credential_leak(gateway_client):
    """review_diff detects the password logging vulnerability and fails the diff."""
    result = await gateway_client.call_tool("review_diff", {"diff_text": SAMPLE_DIFF})
    assert result["verdict"] == "fail"
    assert any(f["severity"] == "CRITICAL" for f in result["findings"])
