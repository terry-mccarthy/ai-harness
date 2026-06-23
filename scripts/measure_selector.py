"""Token measurement and comparison reporting for the model-selector demo.

Usage (standalone):
    from measure_selector import TokenLedger, comparison_report

    selected = TokenLedger("selected")
    selected.record("runbook lookup", "low", "haiku", 1000, 80)
    ...
    baseline = TokenLedger("baseline")
    baseline.record("runbook lookup", "low", "opus", 1500, 200)
    ...
    print(comparison_report(selected, baseline))

Pricing dict (optional, per million tokens):
    {"haiku": {"prompt": 0.80, "completion": 4.00}, ...}
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class TaskRecord:
    name: str
    tier: str
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class TokenLedger:
    def __init__(self, run_label: str = "selected"):
        self.run_label = run_label
        self.records: list[TaskRecord] = []

    def record(
        self,
        name: str,
        tier: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        self.records.append(TaskRecord(name, tier, model, prompt_tokens, completion_tokens))

    def total_prompt(self) -> int:
        return sum(r.prompt_tokens for r in self.records)

    def total_completion(self) -> int:
        return sum(r.completion_tokens for r in self.records)

    def total_tokens(self) -> int:
        return self.total_prompt() + self.total_completion()

    def to_dict(self) -> dict:
        return {
            "run_label": self.run_label,
            "total_prompt": self.total_prompt(),
            "total_completion": self.total_completion(),
            "total_tokens": self.total_tokens(),
            "records": [asdict(r) for r in self.records],
        }


def _cost(records: list[TaskRecord], pricing: dict) -> float:
    total = 0.0
    for r in records:
        p = pricing.get(r.model, {})
        total += r.prompt_tokens * p.get("prompt", 0.0) / 1_000_000
        total += r.completion_tokens * p.get("completion", 0.0) / 1_000_000
    return total


def _fmt(n: int | str) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def comparison_report(
    selected: TokenLedger,
    baseline: TokenLedger,
    pricing: dict | None = None,
) -> str:
    col_w = [28, 8, 28, 8, 8, 8]
    sep = "─" * sum(col_w)

    def row(name, tier, model, prompt, compl, total, indent="  "):
        model_short = model.split("/")[-1] if "/" in model else model
        return (
            f"{indent}{name:<{col_w[0]}}"
            f"{tier:<{col_w[1]}}"
            f"{model_short:<{col_w[2]}}"
            f"{_fmt(prompt):>{col_w[3]}}"
            f"{_fmt(compl):>{col_w[4]}}"
            f"{_fmt(total):>{col_w[5]}}"
        )

    header = row("Task", "Tier", "Model", "Prompt", "Compl", "Total", indent="")
    lines = [sep, header, sep]

    for label, ledger in [("SELECTED", selected), ("BASELINE", baseline)]:
        lines.append(f"{label}")
        for r in ledger.records:
            lines.append(row(r.name, r.tier, r.model, r.prompt_tokens, r.completion_tokens, r.total_tokens))
        lines.append("")

    # Summary
    sel_total = selected.total_tokens()
    base_total = baseline.total_tokens()
    saved = base_total - sel_total
    pct = (saved / base_total * 100) if base_total else 0.0

    lines.append(sep)
    lines.append("SUMMARY")
    lines.append(f"  {'Selected total':<38}{_fmt(selected.total_prompt()):>8}{_fmt(selected.total_completion()):>8}{_fmt(sel_total):>8}")
    lines.append(f"  {'Baseline total':<38}{_fmt(baseline.total_prompt()):>8}{_fmt(baseline.total_completion()):>8}{_fmt(base_total):>8}")
    lines.append(f"  {'Tokens saved':<38}{'':>8}{'':>8}{_fmt(saved):>8}  ({pct:.1f}%)")

    if pricing:
        sel_cost = _cost(selected.records, pricing)
        base_cost = _cost(baseline.records, pricing)
        cost_saved = base_cost - sel_cost
        lines.append(f"  {'Est. cost saved (USD)':<54}${cost_saved:.4f}")

    lines.append(sep)
    return "\n".join(lines)
