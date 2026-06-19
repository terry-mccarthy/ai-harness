import logging

from .models import GateSignal, Violation
from .registry import REGISTRY

logger = logging.getLogger(__name__)


async def run_gate(repo_path: str, target_language: str) -> GateSignal:
    """Run all checkers for the target language and return the aggregate signal."""
    checkers = REGISTRY.get(target_language, [])
    all_violations: list[Violation] = []

    for checker in checkers:
        try:
            violations = await checker.check(repo_path)
        except Exception:
            logger.warning("checker %s failed for %s", type(checker).__name__, repo_path, exc_info=True)
            continue

        all_violations.extend(violations)
        if any(v.severity == "HARD" for v in violations):
            return GateSignal("FAIL", all_violations, "STOP_AND_SURFACE")

    if all_violations:
        return GateSignal("FAIL", all_violations, "PROCEED")

    return GateSignal("PASS", [], "PROCEED")
