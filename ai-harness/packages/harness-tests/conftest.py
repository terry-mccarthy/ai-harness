import pytest
import os
from ollama import AsyncClient
from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent


@pytest.fixture
def gateway_client():
    return GatewayClient(
        gateway_url=os.environ["MCPJUNGLE_URL"],
        client_id="code-reviewer",
        client_secret=os.environ["CODE_REVIEWER_SECRET"],
    )


@pytest.fixture
def reviewer_agent(gateway_client):
    return CodeReviewerAgent(
        gateway=gateway_client,
        llm_client=AsyncClient(host=os.environ.get("OLLAMA_HOST", "http://localhost:11434")),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder"),
    )
