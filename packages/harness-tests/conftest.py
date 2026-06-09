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


# FakeEmbedder for deterministic clustering tests
import hashlib
import numpy as np


class FakeEmbedder:
    """Generate deterministic embeddings for testing.

    - Extracts key terms (content words) from text
    - Uses those terms to create a deterministic sparse vector
    - Similar text (shared terms) → similar vectors
    - Identical text → identical vectors
    """
    def __init__(self, dim: int = 3584):
        self.dim = dim
        self._term_cache = {}

    async def embed(self, text: str) -> np.ndarray:
        """Generate deterministic embedding based on primary topic."""
        t_lower = text.lower()

        # Classify by primary topic (for test clustering)
        if "connection" in t_lower or "pool" in t_lower or "db" in t_lower:
            topic_id = "database-connection"
        elif "feature" in t_lower or "deployment" in t_lower or "release" in t_lower:
            topic_id = "deployment"
        else:
            topic_id = hashlib.md5(text.encode()).hexdigest()

        # Create vector: same topic → same seed → same vector (high similarity)
        seed = int(hashlib.md5(topic_id.encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        vec = rng.randn(self.dim).astype(np.float32)
        return vec / np.linalg.norm(vec)


@pytest.fixture
def fake_embedder():
    """Provide a FakeEmbedder for deterministic embedding tests."""
    return FakeEmbedder(dim=3584)


@pytest.fixture
async def memory_store_with_fake_embedder(fake_embedder):
    """Memory store using FakeEmbedder for deterministic clustering tests.

    Patches the _embed method to use deterministic hashing instead of Ollama.
    """
    from harness_memory.memory_store import PostgresMemoryStore

    PG_DSN = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

    store = PostgresMemoryStore(PG_DSN, REDIS_URL, "fake-model", "http://fake")

    # Patch the _embed method to use FakeEmbedder
    store._embed = fake_embedder.embed

    await store.setup()
    yield store
    await store._truncate()
    await store.close()


@pytest.fixture
async def consolidation_worker_with_fake_embedder(memory_store_with_fake_embedder):
    """Consolidation worker with FakeEmbedder for deterministic tests."""
    from harness_memory.formula_store import DoltFormulaStore
    from harness_memory.consolidation import ConsolidationWorker

    DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
    DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))

    fstore = DoltFormulaStore(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root", database="harness"
    )
    return ConsolidationWorker(store=memory_store_with_fake_embedder, formula_store=fstore)
