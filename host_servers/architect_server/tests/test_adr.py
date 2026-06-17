"""Unit tests for architect_server.adr — filesystem-backed ADR store."""
from pathlib import Path

import pytest

from architect_server.adr import list_adrs, read_adr, write_adr


def _write_adr(root: Path, name: str, title: str, status: str = "accepted") -> None:
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    (adr_dir / name).write_text(
        f"# {title}\n\n**Status:** {status}\n\n## Context\n\nSome context here.\n"
    )


@pytest.fixture
def adr_repo(tmp_path: Path) -> Path:
    _write_adr(tmp_path, "0001-use-postgres.md", "ADR-0001: Use Postgres for app data")
    _write_adr(tmp_path, "0002-event-bus.md", "ADR-0002: Adopt Kafka event bus")
    _write_adr(tmp_path, "0036-architect-mcp.md", "ADR-0036: Architect MCP server", status="proposed")
    return tmp_path


def test_list_adrs_returns_all_records(adr_repo: Path):
    adrs = list_adrs(adr_repo)
    ids = [a["id"] for a in adrs]
    assert ids == ["0001", "0002", "0036"]
    assert adrs[0]["title"] == "ADR-0001: Use Postgres for app data"
    assert adrs[0]["status"] == "accepted"
    assert adrs[2]["status"] == "proposed"


def test_read_adr_by_query_matches_title(adr_repo: Path):
    result = read_adr(adr_repo, query="kafka event bus")
    assert len(result) >= 1
    assert result[0]["id"] == "0002"
    assert "Kafka" in result[0]["content"]


def test_read_adr_by_path(adr_repo: Path):
    result = read_adr(adr_repo, path="docs/adr/0036-architect-mcp.md")
    assert len(result) == 1
    assert result[0]["id"] == "0036"
    assert result[0]["status"] == "proposed"


def test_read_adr_missing_path_returns_empty(adr_repo: Path):
    result = read_adr(adr_repo, path="docs/adr/9999-nope.md")
    assert result == []


def test_read_adr_no_query_returns_all(adr_repo: Path):
    result = read_adr(adr_repo)
    assert len(result) == 3


def test_write_adr_assigns_next_number(adr_repo: Path):
    out = write_adr(adr_repo, title="Switch to gRPC for service mesh", content="Body of decision.")
    assert out["id"] == "0037"
    assert out["path"] == "docs/adr/0037-switch-to-grpc-for-service-mesh.md"
    written = (adr_repo / out["path"]).read_text()
    assert "Switch to gRPC for service mesh" in written
    assert "Body of decision." in written


def test_write_adr_slugifies_title(adr_repo: Path):
    out = write_adr(adr_repo, title="Some/Crazy Title!! With $$ punctuation", content="x")
    assert out["path"].endswith("-some-crazy-title-with-punctuation.md")


def test_write_adr_into_empty_repo(tmp_path: Path):
    out = write_adr(tmp_path, title="First decision", content="body")
    assert out["id"] == "0001"
    assert (tmp_path / out["path"]).exists()


def test_round_trip_write_then_read(adr_repo: Path):
    out = write_adr(adr_repo, title="Use BM25 for codebase search", content="Rationale...")
    found = read_adr(adr_repo, query="BM25 codebase search")
    ids = [a["id"] for a in found]
    assert out["id"] in ids
