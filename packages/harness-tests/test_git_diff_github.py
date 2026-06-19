"""Unit tests for diff_proxy_server GitHub PR mode.

Tests are written against _fetch_github_pr_diff and the git_diff tool directly.
No Docker stack, no network calls — urllib is mocked throughout.
"""
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# diff_proxy_server.py is not an installable package — insert its directory
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "stub_servers"))

import diff_proxy_server  # noqa: E402


# ---------------------------------------------------------------------------
# _fetch_github_pr_diff — unit tests
# ---------------------------------------------------------------------------

SAMPLE_PR_DIFF = """\
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,3 +1,4 @@
+    print("hello")
 def login(): pass
"""


def _mock_urlopen(body: str, status: int = 200):
    """Return a context-manager mock that yields a file-like response."""
    resp = MagicMock()
    resp.read.return_value = body.encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    cm = MagicMock()
    cm.__enter__ = lambda s: resp
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_fetch_github_pr_diff_calls_correct_url():
    with patch("diff_proxy_server.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_urlopen(SAMPLE_PR_DIFF)
        diff_proxy_server._fetch_github_pr_diff("owner/repo", 42, token=None)

    req = mock_open.call_args[0][0]
    assert "owner/repo/pulls/42" in req.full_url


def test_fetch_github_pr_diff_sets_diff_accept_header():
    with patch("diff_proxy_server.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_urlopen(SAMPLE_PR_DIFF)
        diff_proxy_server._fetch_github_pr_diff("owner/repo", 42, token=None)

    req = mock_open.call_args[0][0]
    assert req.get_header("Accept") == "application/vnd.github.v3.diff"


def test_fetch_github_pr_diff_includes_auth_header_when_token_given():
    with patch("diff_proxy_server.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_urlopen(SAMPLE_PR_DIFF)
        diff_proxy_server._fetch_github_pr_diff("owner/repo", 42, token="ghp_abc123")

    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer ghp_abc123"


def test_fetch_github_pr_diff_omits_auth_header_when_no_token():
    with patch("diff_proxy_server.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_urlopen(SAMPLE_PR_DIFF)
        diff_proxy_server._fetch_github_pr_diff("owner/repo", 42, token=None)

    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") is None


def test_fetch_github_pr_diff_returns_decoded_body():
    with patch("diff_proxy_server.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _mock_urlopen(SAMPLE_PR_DIFF)
        result = diff_proxy_server._fetch_github_pr_diff("owner/repo", 42, token=None)

    assert result == SAMPLE_PR_DIFF


# ---------------------------------------------------------------------------
# git_diff tool — github mode routing
# ---------------------------------------------------------------------------

def test_git_diff_github_mode_returns_pr_diff():
    with patch("diff_proxy_server._fetch_github_pr_diff", return_value=SAMPLE_PR_DIFF):
        result = diff_proxy_server.git_diff(
            github_repo="owner/repo",
            pr_number=42,
        )
    assert result["diff"] == SAMPLE_PR_DIFF
    assert result["source"] == "github"


def test_git_diff_github_mode_passes_env_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-xyz")
    with patch("diff_proxy_server._fetch_github_pr_diff", return_value=SAMPLE_PR_DIFF) as mock_fetch:
        diff_proxy_server.git_diff(github_repo="owner/repo", pr_number=7)
    mock_fetch.assert_called_once_with("owner/repo", 7, token="env-token-xyz")


def test_git_diff_github_mode_missing_repo_raises():
    with pytest.raises(ValueError, match="github_repo"):
        diff_proxy_server.git_diff(pr_number=42)


def test_git_diff_diff_text_takes_precedence_over_github():
    """diff_text shortcut wins even when pr_number is also supplied."""
    with patch("diff_proxy_server._fetch_github_pr_diff") as mock_fetch:
        result = diff_proxy_server.git_diff(diff_text="already-have-it", pr_number=1, github_repo="x/y")
    mock_fetch.assert_not_called()
    assert result["source"] == "passthrough"


# ---------------------------------------------------------------------------
# _fetch_github_pr_diff — error handling
# ---------------------------------------------------------------------------

def test_fetch_github_pr_diff_http_error_raises_value_error():
    """HTTPError (e.g. 404 private repo, 401 bad token) is wrapped in ValueError."""
    import urllib.error
    http_err = urllib.error.HTTPError(
        url="https://api.github.com/repos/x/y/pulls/1",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    with patch("diff_proxy_server.urllib.request.urlopen", side_effect=http_err):
        with pytest.raises(ValueError, match="404"):
            diff_proxy_server._fetch_github_pr_diff("x/y", 1, token=None)


def test_fetch_github_pr_diff_url_error_raises_value_error():
    """URLError (network failure) is wrapped in ValueError."""
    import urllib.error
    with patch("diff_proxy_server.urllib.request.urlopen",
               side_effect=urllib.error.URLError("Name or service not known")):
        with pytest.raises(ValueError, match="network"):
            diff_proxy_server._fetch_github_pr_diff("x/y", 1, token=None)


# ---------------------------------------------------------------------------
# github_repo format validation
# ---------------------------------------------------------------------------

def test_git_diff_invalid_github_repo_format_raises():
    """github_repo must be 'owner/repo'; bare names or paths are rejected."""
    with pytest.raises(ValueError, match="owner/repo"):
        diff_proxy_server.git_diff(pr_number=1, github_repo="notavalidrepo")


def test_git_diff_valid_github_repo_format_accepted():
    with patch("diff_proxy_server._fetch_github_pr_diff", return_value=SAMPLE_PR_DIFF):
        result = diff_proxy_server.git_diff(pr_number=1, github_repo="owner/repo")
    assert result["source"] == "github"
