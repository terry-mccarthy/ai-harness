"""Unit tests for TokenLedger and comparison_report in measure_selector.

No LLM calls or network access — pure computation on token counts.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))

import pytest
from measure_selector import TaskRecord, TokenLedger, comparison_report


# ---------------------------------------------------------------------------
# TaskRecord
# ---------------------------------------------------------------------------

def test_task_record_total():
    r = TaskRecord("check runbook", "low", "haiku", 1000, 100)
    assert r.total_tokens == 1100


# ---------------------------------------------------------------------------
# TokenLedger.record and totals
# ---------------------------------------------------------------------------

def test_ledger_empty_totals():
    ledger = TokenLedger("selected")
    assert ledger.total_prompt() == 0
    assert ledger.total_completion() == 0
    assert ledger.total_tokens() == 0


def test_ledger_accumulates():
    ledger = TokenLedger("selected")
    ledger.record("task-a", "low", "haiku", 500, 50)
    ledger.record("task-b", "high", "opus", 2000, 400)
    assert ledger.total_prompt() == 2500
    assert ledger.total_completion() == 450
    assert ledger.total_tokens() == 2950


def test_ledger_records_stored():
    ledger = TokenLedger("selected")
    ledger.record("task-a", "low", "haiku", 500, 50)
    assert len(ledger.records) == 1
    assert ledger.records[0].name == "task-a"
    assert ledger.records[0].tier == "low"
    assert ledger.records[0].model == "haiku"


# ---------------------------------------------------------------------------
# comparison_report — structure and savings
# ---------------------------------------------------------------------------

def _make_pair():
    selected = TokenLedger("selected")
    selected.record("runbook lookup", "low", "haiku", 1000, 80)
    selected.record("log grep", "medium", "sonnet", 2000, 200)
    selected.record("incident RCA", "high", "opus", 4000, 800)

    baseline = TokenLedger("baseline")
    baseline.record("runbook lookup", "low", "opus", 1500, 200)
    baseline.record("log grep", "medium", "opus", 2800, 400)
    baseline.record("incident RCA", "high", "opus", 4200, 900)
    return selected, baseline


def test_comparison_report_contains_both_labels(capsys):
    selected, baseline = _make_pair()
    report = comparison_report(selected, baseline)
    assert "SELECTED" in report
    assert "BASELINE" in report


def test_comparison_report_shows_savings():
    selected, baseline = _make_pair()
    report = comparison_report(selected, baseline)
    # selected total = 1080+2200+4800 = 8080; baseline = 1700+3200+5100 = 10000
    assert "8,080" in report
    assert "10,000" in report
    assert "19.2%" in report or "19%" in report  # savings %


def test_comparison_report_no_savings_when_equal():
    ledger = TokenLedger("selected")
    ledger.record("task", "high", "opus", 1000, 100)
    baseline = TokenLedger("baseline")
    baseline.record("task", "high", "opus", 1000, 100)
    report = comparison_report(ledger, baseline)
    assert "0.0%" in report or "0%" in report


def test_comparison_report_task_names_present():
    selected, baseline = _make_pair()
    report = comparison_report(selected, baseline)
    assert "runbook lookup" in report
    assert "incident RCA" in report


# ---------------------------------------------------------------------------
# Cost estimation (optional pricing block)
# ---------------------------------------------------------------------------

def test_comparison_report_cost_shown_when_pricing_provided():
    selected, baseline = _make_pair()
    pricing = {
        "haiku":  {"prompt": 0.80, "completion": 4.00},
        "sonnet": {"prompt": 3.00, "completion": 15.00},
        "opus":   {"prompt": 15.00, "completion": 75.00},
    }
    report = comparison_report(selected, baseline, pricing=pricing)
    assert "$" in report


def test_comparison_report_no_cost_without_pricing():
    selected, baseline = _make_pair()
    report = comparison_report(selected, baseline)
    assert "$" not in report


# ---------------------------------------------------------------------------
# to_dict round-trip
# ---------------------------------------------------------------------------

def test_ledger_to_dict():
    ledger = TokenLedger("selected")
    ledger.record("task-a", "low", "haiku", 500, 50)
    d = ledger.to_dict()
    assert d["run_label"] == "selected"
    assert d["total_tokens"] == 550
    assert len(d["records"]) == 1
    assert d["records"][0]["name"] == "task-a"
