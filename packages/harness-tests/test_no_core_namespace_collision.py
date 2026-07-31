"""Regression test for issue #02 (namespace-core-packages-per-service).

`services/governance/` and `services/review_server/` each used to (or, for
review_server, will once issue #06 lands on this lineage) carve their
shared helpers into a sibling package literally named `core`. Because both
services are separate containers in production, a bare `import core.X` /
`from core.X import ...` inside each service works fine there — only one
`core` is ever on `sys.path` per process.

That assumption breaks in `packages/harness-tests/`, where unit tests insert
a service directory straight onto `sys.path` (see e.g.
`test_unit_bootstrap_mcp_tool.py`, `test_unit_chain_adversarial_verdict.py`)
to import service internals directly, without a real package install. If two
such tests inserted different services' directories and both did a bare
`import core`, the second import would silently resolve to whichever
service's `core` loaded first (`sys.modules["core"]` is a single global slot)
instead of raising an ImportError — easy to misdiagnose as broken app logic.

Fix: each service's shared-helpers package now has a unique name
(`governance_core`, `review_server_core`) so a bare import is never
ambiguous. This test proves both can be imported in the same interpreter
process without one shadowing the other.

Note on module choice: we deliberately resolve submodules via
`importlib.util.find_spec` (which locates a module without executing it)
rather than importing `governance_core.metrics` / a real config module
outright. Actually *executing* `governance_core.metrics` alongside
`review_server`'s (unrelated, top-level, non-`core`) `metrics.py` trips a
pre-existing Prometheus duplicate-timeseries collision — both define a
counter literally named `harness_llm_calls_total` — whenever some other
test in the same session already loaded `review_server/server.py`. That
metric-name collision is a distinct, pre-existing bug (tracked separately,
see issue #03) unrelated to package *naming*; this test should not become
flaky because of it. `governance_core.config` similarly can't be imported
bare in a unit-test process without `JWT_PRIVATE_KEY` / `CODE_REVIEWER_SECRET`
set. `find_spec` sidesteps both: it proves the real submodule file resolves
under the correct package without executing any module-level side effects.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE_DIR = _REPO_ROOT / "services" / "governance"
_REVIEW_SERVER_DIR = _REPO_ROOT / "services" / "review_server"


def _on_path(dir_path: Path) -> None:
    str_path = str(dir_path)
    if str_path not in sys.path:
        sys.path.insert(0, str_path)


def _spec_origin(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin is not None, f"{module_name} did not resolve"
    return Path(spec.origin).resolve()


def test_governance_core_importable_under_unique_name():
    """governance's shared-helpers package imports cleanly as `governance_core`."""
    _on_path(_GOVERNANCE_DIR)
    import governance_core

    assert governance_core.__name__ == "governance_core"
    assert Path(governance_core.__file__).resolve() == (
        _GOVERNANCE_DIR / "governance_core" / "__init__.py"
    ).resolve()
    # A real submodule resolves under the renamed package (without executing it).
    assert _spec_origin("governance_core.metrics") == (
        _GOVERNANCE_DIR / "governance_core" / "metrics.py"
    ).resolve()


@pytest.mark.skipif(
    not (_REVIEW_SERVER_DIR / "review_server_core").is_dir(),
    reason=(
        "services/review_server/review_server_core does not exist on this "
        "branch lineage yet (depends on issue #06's config-extraction commit "
        "landing here) — see the issue #02 completion report for details."
    ),
)
def test_review_server_core_importable_under_unique_name():
    """review_server's shared-helpers package imports cleanly as `review_server_core`."""
    _on_path(_REVIEW_SERVER_DIR)
    import review_server_core

    assert review_server_core.__name__ == "review_server_core"
    assert Path(review_server_core.__file__).resolve() == (
        _REVIEW_SERVER_DIR / "review_server_core" / "__init__.py"
    ).resolve()
    assert _spec_origin("review_server_core.config") == (
        _REVIEW_SERVER_DIR / "review_server_core" / "config.py"
    ).resolve()


@pytest.mark.skipif(
    not (_REVIEW_SERVER_DIR / "review_server_core").is_dir(),
    reason=(
        "services/review_server/review_server_core does not exist on this "
        "branch lineage yet (depends on issue #06's config-extraction commit "
        "landing here) — see the issue #02 completion report for details."
    ),
)
def test_governance_core_and_review_server_core_do_not_shadow_each_other():
    """The actual regression this issue exists to prevent.

    Both services' shared-helper packages land on `sys.path` in the same
    pytest process (mirroring how independent unit test files each insert
    their own service directory). Neither may bind the ambiguous name
    `core` in `sys.modules`, and the two packages — and their same-named
    submodules — must resolve to distinct, correct files.
    """
    _on_path(_GOVERNANCE_DIR)
    _on_path(_REVIEW_SERVER_DIR)

    import governance_core
    import review_server_core

    assert "core" not in sys.modules
    assert sys.modules["governance_core"] is governance_core
    assert sys.modules["review_server_core"] is review_server_core
    assert governance_core is not review_server_core

    # Both packages happen to define a same-named `config` submodule — the
    # exact ambiguity a bare `core` package name would have collapsed.
    governance_config = _spec_origin("governance_core.config")
    review_server_config = _spec_origin("review_server_core.config")
    assert governance_config != review_server_config
    assert governance_config == (_GOVERNANCE_DIR / "governance_core" / "config.py").resolve()
    assert review_server_config == (
        _REVIEW_SERVER_DIR / "review_server_core" / "config.py"
    ).resolve()
