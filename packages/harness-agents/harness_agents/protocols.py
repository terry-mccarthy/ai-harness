from typing import Protocol, runtime_checkable
from .types import AgentState


@runtime_checkable
class AgentNode(Protocol):
    name: str
    allowed_tools: list[str]
    memory_namespace: str

    async def run(self, state: AgentState) -> AgentState: ...
