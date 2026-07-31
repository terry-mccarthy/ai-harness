"""OPA policy decision helper."""
import logging
from typing import Any

import httpx

from .config import OPA_URL

logger = logging.getLogger(__name__)


async def check_opa(rule_path: str, input_data: dict) -> Any:
    """Query an OPA policy rule and return the raw `result` value, or None on error.

    `rule_path` is the path under `/v1/data/`, e.g. `"harness/allow"`. Callers
    are responsible for interpreting the returned value (truthy bool, exact
    `is True`, list membership, etc.) since different rules have different
    return shapes.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OPA_URL}/v1/data/{rule_path}",
                json={"input": input_data},
                timeout=5.0,
            )
        return resp.json().get("result")
    except Exception as e:
        logger.error("OPA call failed (%s): %s", rule_path, e)
        return None
