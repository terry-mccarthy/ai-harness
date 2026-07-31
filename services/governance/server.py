"""Governance service entry point.

All endpoints live in `routers/`. This module wires the FastAPI app and
mounts each router. Shared helpers live in `governance_core/`.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from governance_core.dolt import close_pool, init_pool
from routers.agents import router as agents_router
from routers.learning import router as learning_router
from routers.policy import router as policy_router
from routers.skills import router as skills_router
from routers.tasks import router as tasks_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly create the shared async Dolt pool so the first request doesn't
    # pay the connect cost. This is a warm-up, not a requirement for
    # correctness: write_audit/write_episode/write_gate_failure lazily call
    # init_pool() themselves, so a transient failure here must not crash the
    # whole service — /check and other Dolt-independent endpoints still need
    # to come up.
    try:
        await init_pool()
    except Exception as e:
        logger.error("Dolt pool warm-up failed at startup (will retry lazily): %s", e)
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan)
app.include_router(policy_router)
app.include_router(agents_router)
app.include_router(learning_router)
app.include_router(skills_router)
app.include_router(tasks_router)
