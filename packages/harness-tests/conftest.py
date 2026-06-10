import pytest
import os
import httpx
from pathlib import Path
from dotenv import load_dotenv

# Load .env from repo root so tests work without `source .env` in the shell
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")


@pytest.fixture
def gateway_client():
    url = os.environ.get("MCPJUNGLE_URL", GOVERNANCE_URL)
    return GatewayClient(
        gateway_url=url,
        client_id="code-reviewer",
        client_secret=os.environ["CODE_REVIEWER_SECRET"],
    )


from harness_agents.llm import OllamaProvider

@pytest.fixture
def reviewer_agent(gateway_client):
    return CodeReviewerAgent(
        gateway=gateway_client,
        llm_provider=OllamaProvider(
            host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder"),
        ),
    )


@pytest.fixture
async def code_reviewer_token():
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GOVERNANCE_URL}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "code-reviewer",
                "client_secret": os.environ["CODE_REVIEWER_SECRET"],
            },
        )
    resp.raise_for_status()
    return resp.json()["access_token"]


import numpy as np
