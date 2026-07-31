"""Regression test for issue #03 (per-service-prometheus-registry).

`services/governance/governance_core/metrics.py` and
`services/review_server/metrics.py` each define module-level Prometheus
`Counter`/`Histogram` objects with identical names (`harness_llm_calls_total`,
`harness_llm_tokens_total`) and identical label sets. Both used to register
into `prometheus_client`'s implicit global default `CollectorRegistry`
(`prometheus_client.REGISTRY`). In production each service is its own
process, so this collision is invisible — but importing both metrics modules
into a single Python process (e.g. this shared pytest session) used to raise
`ValueError: Duplicated timeseries in CollectorRegistry`.

See `test_no_core_namespace_collision.py`'s module docstring, which
deliberately avoids executing `governance_core.metrics` for exactly this
reason and points here as the fix.

Fix: each service now owns its own explicit `CollectorRegistry` instead of
relying on the shared implicit default, so both modules can be imported (and
their metrics scraped) side by side without collision.
"""
import sys
from pathlib import Path

from prometheus_client import generate_latest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE_DIR = _REPO_ROOT / "services" / "governance"
_REVIEW_SERVER_DIR = _REPO_ROOT / "services" / "review_server"


def _on_path(dir_path: Path) -> None:
    str_path = str(dir_path)
    if str_path not in sys.path:
        sys.path.insert(0, str_path)


def test_both_metrics_modules_import_together_without_collision():
    """Importing both services' metrics modules in one process must not raise.

    This is the actual regression: before the fix, the second import below
    raised `ValueError: Duplicated timeseries in CollectorRegistry` because
    both modules registered `harness_llm_calls_total` /
    `harness_llm_tokens_total` (same names, same labels) into the same
    implicit global `prometheus_client.REGISTRY`.
    """
    _on_path(_GOVERNANCE_DIR)
    import governance_core.metrics as governance_metrics

    _on_path(_REVIEW_SERVER_DIR)
    import metrics as review_server_metrics

    # Each service now owns a distinct, explicit registry.
    assert governance_metrics.REGISTRY is not review_server_metrics.REGISTRY

    # Both still expose the same metric *names* in their exposition text
    # (dashboards/PromQL depend on this) — only the in-process registry
    # object changed, not the names or labels rendered at scrape time.
    governance_exposition = generate_latest(governance_metrics.REGISTRY).decode()
    review_server_exposition = generate_latest(review_server_metrics.REGISTRY).decode()

    assert "harness_llm_calls_total" in governance_exposition
    assert "harness_llm_calls_total" in review_server_exposition
    assert "harness_llm_tokens_total" in governance_exposition
    assert "harness_llm_tokens_total" in review_server_exposition
