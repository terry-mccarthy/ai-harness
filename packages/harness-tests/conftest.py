import pytest
import os
import httpx
from pathlib import Path
from dotenv import load_dotenv

# Load .env from repo root so tests work without `source .env` in the shell
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Test suite runs with the committed dev-default secrets (JWT_SECRET, governance's
# test RSA key, etc.) — declare that explicitly so the import-time tripwires in
# harness_supervisor.graph and services/governance/core/config.py don't fire.
# setdefault: an explicit ENV from the shell or .env still wins.
os.environ.setdefault("ENV", "test")

from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
MCPJUNGLE_URL = os.environ.get("MCPJUNGLE_URL", "http://localhost:8080")


@pytest.fixture
def gateway_client():
    return GatewayClient(
        gateway_url=MCPJUNGLE_URL,
        governance_url=GOVERNANCE_URL,
        client_id="code-reviewer",
        client_secret=os.environ["CODE_REVIEWER_SECRET"],
    )


@pytest.fixture(scope="module")
def module_gateway_client():
    """Module-scoped gateway client for tests that share a single LLM call."""
    return GatewayClient(
        gateway_url=MCPJUNGLE_URL,
        governance_url=GOVERNANCE_URL,
        client_id="code-reviewer",
        client_secret=os.environ["CODE_REVIEWER_SECRET"],
    )


from harness_agents.llm import build_llm_from_env

@pytest.fixture
def reviewer_agent(gateway_client):
    return CodeReviewerAgent(
        gateway=gateway_client,
        llm_provider=build_llm_from_env(),
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
