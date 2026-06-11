"""Unit tests for linter_server — diff parsing and semgrep output mapping."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# linter_server.py lives in stub_servers/, which isn't an installable package
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "stub_servers"))
from linter_server import _parse_diff, _semgrep_findings, _SEVERITY_MAP  # noqa: E402

SAMPLE_DIFF = """\
diff --git a/auth.py b/auth.py
index 1a2b3c4..5d6e7f8 100644
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,8 @@ def login(username, password):
     user = db.find(username)
+    print(f"Login attempt: {username} {password}")
     if user and user.check_password(password):
         return generate_token(user)
"""

MULTI_FILE_DIFF = """\
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,3 +1,4 @@
+    print("debug")
 def login(): pass
diff --git a/db.py b/db.py
--- a/db.py
+++ b/db.py
@@ -5,3 +5,4 @@
+    cursor.execute(f"SELECT * FROM users WHERE id = '{user_id}'")
 def get(): pass
"""


# ---------------------------------------------------------------------------
# _parse_diff
# ---------------------------------------------------------------------------

def test_parse_diff_extracts_added_lines():
    files = _parse_diff(SAMPLE_DIFF)
    assert "auth.py" in files
    assert 'print(f"Login attempt: {username} {password}")' in files["auth.py"]


def test_parse_diff_excludes_context_and_removed_lines():
    files = _parse_diff(SAMPLE_DIFF)
    content = files["auth.py"]
    assert "user = db.find" not in content      # context line (no +/-)
    assert "return generate_token" not in content  # context line


def test_parse_diff_multiple_files():
    files = _parse_diff(MULTI_FILE_DIFF)
    assert "auth.py" in files
    assert "db.py" in files
    assert 'print("debug")' in files["auth.py"]
    assert "cursor.execute" in files["db.py"]


def test_parse_diff_empty_returns_empty():
    assert _parse_diff("") == {}


def test_parse_diff_no_added_lines():
    diff = """\
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,3 +1,3 @@
-old line
 context line
"""
    assert _parse_diff(diff) == {}


# ---------------------------------------------------------------------------
# _semgrep_findings — subprocess is mocked
# ---------------------------------------------------------------------------

SEMGREP_OUTPUT = {
    "results": [
        {
            "check_id": "local.print-call",
            "path": "/tmp/xyz/auth.py",
            "start": {"line": 3, "col": 4},
            "extra": {
                "message": "print() may expose sensitive data",
                "severity": "WARNING",
            },
        },
        {
            "check_id": "local.hardcoded-credential",
            "path": "/tmp/xyz/config.py",
            "start": {"line": 1, "col": 0},
            "extra": {
                "message": "Hardcoded credential detected",
                "severity": "ERROR",
            },
        },
    ],
    "errors": [],
}


def test_semgrep_findings_maps_severity():
    with patch("linter_server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout=json.dumps(SEMGREP_OUTPUT), stderr=""
        )
        findings = _semgrep_findings("/tmp/xyz")

    assert len(findings) == 2
    severities = {f["severity"] for f in findings}
    assert "WARNING" in severities
    assert "CRITICAL" in severities  # ERROR → CRITICAL


def test_semgrep_findings_includes_rule_and_message():
    with patch("linter_server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout=json.dumps(SEMGREP_OUTPUT), stderr=""
        )
        findings = _semgrep_findings("/tmp/xyz")

    rules = {f["rule"] for f in findings}
    assert "local.print-call" in rules
    assert "local.hardcoded-credential" in rules


def test_semgrep_findings_no_results_returns_empty():
    with patch("linter_server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"results": [], "errors": []}), stderr=""
        )
        findings = _semgrep_findings("/tmp/xyz")
    assert findings == []


def test_semgrep_findings_crash_returns_empty():
    """Non-0/1 exit code (semgrep internal error) → graceful empty result."""
    with patch("linter_server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="fatal error")
        findings = _semgrep_findings("/tmp/xyz")
    assert findings == []


def test_semgrep_findings_bad_json_returns_empty():
    with patch("linter_server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="not json", stderr="")
        findings = _semgrep_findings("/tmp/xyz")
    assert findings == []


# ---------------------------------------------------------------------------
# severity map completeness
# ---------------------------------------------------------------------------

def test_severity_map_covers_all_semgrep_levels():
    for level in ("ERROR", "WARNING", "INFO"):
        assert level in _SEVERITY_MAP
