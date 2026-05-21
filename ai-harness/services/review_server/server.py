import logging
import os
import uvicorn
from mcp.server.fastmcp import FastMCP

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
from mcp.server.transport_security import TransportSecuritySettings
from ollama import AsyncClient
from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState

mcp = FastMCP(
    "review_server",
    host="0.0.0.0",
    port=9003,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def review_diff(
    diff_text: str,
    task: str = (
        "Review this diff for: "
        "(1) security vulnerabilities — credential leaks, injection flaws, path traversal, missing auth enforcement, insecure defaults; "
        "(2) code quality — error handling gaps, dead code, resource leaks, incorrect types, silent failures; "
        "(3) architectural concerns — hardcoded values, tight coupling, shared mutable state, missing abstractions. "
        "Report every finding with file, line, severity (CRITICAL/WARNING/INFO), and a specific fix suggestion. "
        "Verdict is 'fail' if any CRITICAL finding exists."
    ),
) -> dict:
    """Run the governed code-reviewer agent and return structured findings."""
    gateway = GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        client_id="code-reviewer",
        client_secret=os.environ.get("CODE_REVIEWER_SECRET", ""),
    )
    llm = AsyncClient(host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

    agent = CodeReviewerAgent(gateway=gateway, llm_client=llm, model=model)
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
