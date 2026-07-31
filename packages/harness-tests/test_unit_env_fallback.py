"""Unit tests for conftest's .env / .env.example fallback loading.

A fresh git worktree (this repo's convention for AFK issue work, see
docs/agents/issue-tracker.md) has no `.env` -- it's gitignored and isn't
copied by `git worktree add`. `.env.example` already documents safe
dev/test defaults for the secrets tests need (e.g. CODE_REVIEWER_SECRET),
so conftest.py should fall back to it instead of letting bracket-access
`os.environ[...]` lookups raise KeyError.

These tests exercise the extracted `load_env_with_fallback` helper
directly against a scratch directory, rather than relying on conftest's
module-level side effect, so each test can control exactly which files
and env vars are present.
"""
import os

from conftest import load_env_with_fallback


def test_fallback_loads_env_example_when_env_missing(tmp_path, monkeypatch):
    """With no .env file present, values come from .env.example."""
    monkeypatch.delenv("CODE_REVIEWER_SECRET", raising=False)
    (tmp_path / ".env.example").write_text("CODE_REVIEWER_SECRET=change-me-in-dev\n")
    # deliberately no .env written -- simulates a fresh worktree

    load_env_with_fallback(tmp_path)

    assert os.environ["CODE_REVIEWER_SECRET"] == "change-me-in-dev"


def test_env_takes_precedence_over_env_example(tmp_path, monkeypatch):
    """When .env IS present, its values win over .env.example's defaults."""
    monkeypatch.delenv("CODE_REVIEWER_SECRET", raising=False)
    (tmp_path / ".env").write_text("CODE_REVIEWER_SECRET=real-secret-from-dotenv\n")
    (tmp_path / ".env.example").write_text("CODE_REVIEWER_SECRET=change-me-in-dev\n")

    load_env_with_fallback(tmp_path)

    assert os.environ["CODE_REVIEWER_SECRET"] == "real-secret-from-dotenv"
