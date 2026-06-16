from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Formula:
    id: str
    name: str
    agent_role: str
    input_schema: dict
    steps: list
    output_contract: dict
    promoted_by: str
    version: int = 1
    status: str = "active"
    description: str = ""
    source_candidate_id: str | None = None
    expires_at: datetime | None = None
    revoked_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ConsolidationResult:
    semantic_items_created: int = 0
    episodes_consolidated: int = 0
    items_pruned: int = 0
    formulas_updated: int = 0
