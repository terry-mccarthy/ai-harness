from abc import ABC, abstractmethod

from .models import Violation


class Checker(ABC):
    """A single deterministic check (e.g. layer enforcement, complexity cap)."""

    @abstractmethod
    async def check(self, repo_path: str) -> list[Violation]:
        ...
