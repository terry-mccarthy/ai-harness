import pytest
import jsonschema
from uuid import uuid4
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA
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
async def test_reviewer_produces_structured_output(reviewer_agent):
    """Core contract: diff in → validated structured output."""
    state = AgentState(
        task="Review this diff for security and quality issues.",
        diff=SAMPLE_DIFF,
        thread_id=str(uuid4()),
        agent_output=None,
        requires_human_approval=False,
        error=None,
    )
    result = await reviewer_agent.run(state)

    output = result["agent_output"]
    assert output is not None

    jsonschema.validate(output, REVIEWER_OUTPUT_SCHEMA)

    assert output["verdict"] == "fail"
    assert len(output["findings"]) > 0


@pytest.mark.integration
async def test_tool_calls_go_through_gateway(reviewer_agent, gateway_client):
    """Governance contract: tool calls are visible in gateway audit log."""
    state = AgentState(
        task="Quick review.",
        diff=SAMPLE_DIFF,
        thread_id=str(uuid4()),
        agent_output=None,
        requires_human_approval=False,
        error=None,
    )
    await reviewer_agent.run(state)

    tool_names = [c["tool"] for c in gateway_client.last_calls]
    assert "git_diff" in tool_names or "run_linter" in tool_names


@pytest.mark.integration
async def test_reviewer_denied_cross_role_tool(gateway_client):
    """Policy contract: code-reviewer token cannot call shell_exec."""
    with pytest.raises(Exception, match="403|Forbidden|ToolAccessDenied|not in allowed"):
        await gateway_client.call_tool("shell_exec", {"command": "ls"})
