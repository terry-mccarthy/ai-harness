import pytest
import os
import httpx
from pathlib import Path
from dotenv import load_dotenv


def load_env_with_fallback(repo_root: Path) -> None:
    """Load .env from repo root, falling back to .env.example for any vars
    it doesn't set.

    A fresh git worktree (this repo's convention for AFK issue work) has no
    `.env` -- it's gitignored and `git worktree add` doesn't copy it. Without
    a fallback, bracket-access lookups like `os.environ["CODE_REVIEWER_SECRET"]`
    raise KeyError, which reads like a real regression rather than a known
    environment gap. `.env.example` already documents safe dev/test defaults
    for these values, so load it too.

    `.env` always wins: load_dotenv's default `override=False` will not
    clobber a var already set in os.environ, so loading .env first and then
    .env.example only fills in gaps -- it never overwrites a real .env value.
    """
    load_dotenv(repo_root / ".env")
    load_dotenv(repo_root / ".env.example")


# Load .env from repo root so tests work without `source .env` in the shell.
# Falls back to .env.example's documented dev defaults when .env is missing
# (e.g. in a fresh worktree) -- see README.md.
load_env_with_fallback(Path(__file__).resolve().parents[2])

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
