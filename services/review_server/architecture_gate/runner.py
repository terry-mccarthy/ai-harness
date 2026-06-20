import logging

from .models import GateSignal, Violation
from .registry import REGISTRY

logger = logging.getLogger(__name__)


def _has_hard(violations: list[Violation]) -> bool:
    return any(v.severity == "HARD" for v in violations)


async def _run_checker(checker, repo_path: str) -> list[Violation]:
    try:
        return await checker.check(repo_path)
    except Exception:
        logger.warning("checker %s failed for %s", type(checker).__name__, repo_path, exc_info=True)
        return []


async def run_gate(repo_path: str, target_language: str) -> GateSignal:
    checkers = REGISTRY.get(target_language, [])
    all_violations: list[Violation] = []

    for checker in checkers:
        violations = await _run_checker(checker, repo_path)
        all_violations.extend(violations)
        if _has_hard(violations):
            return GateSignal("FAIL", all_violations, "STOP_AND_SURFACE")

    if all_violations:
        return GateSignal("FAIL", all_violations, "PROCEED")

    return GateSignal("PASS", [], "PROCEED")
