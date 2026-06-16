"""Governance service entry point.

All endpoints live in `routers/`. This module wires the FastAPI app and
mounts each router. Shared helpers live in `core/`.
"""
import logging
import os

from fastapi import FastAPI

from routers.agents import router as agents_router
from routers.learning import router as learning_router
from routers.policy import router as policy_router
from routers.skills import router as skills_router
from routers.tasks import router as tasks_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

app = FastAPI()
app.include_router(policy_router)
app.include_router(agents_router)
app.include_router(learning_router)
app.include_router(skills_router)
app.include_router(tasks_router)
