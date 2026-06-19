from dataclasses import dataclass, field, asdict


@dataclass
class Violation:
    rule: str
    severity: str  # "HARD" | "SOFT"
    file: str
    message: str


@dataclass
class GateSignal:
    result: str  # "PASS" | "FAIL"
    violations: list[Violation] = field(default_factory=list)
    action: str = "PROCEED"  # "PROCEED" | "STOP_AND_SURFACE"

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "violations": [asdict(v) for v in self.violations],
            "action": self.action,
        }
