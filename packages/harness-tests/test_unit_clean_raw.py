"""Unit tests for _clean_raw — thinking-block and markdown-fence stripping."""

from harness_agents.reviewer import _clean_raw


def test_passthrough_normal_json():
    assert _clean_raw('{"verdict": "pass"}') == '{"verdict": "pass"}'


def test_strips_think_block():
    result = _clean_raw('<think>Some reasoning</think>{"verdict": "pass"}')
    assert result == '{"verdict": "pass"}'


def test_strips_markdown_fences():
    result = _clean_raw('```json\n{"verdict": "pass"}\n```')
    assert result == '{"verdict": "pass"}'


def test_strips_both_think_and_fences():
    result = _clean_raw('<think>reasoning</think>\n```json\n{"verdict": "pass"}\n```')
    assert result == '{"verdict": "pass"}'


def test_handles_only_opening_fence():
    result = _clean_raw('```\n{"verdict": "pass"}')
    assert result == '{"verdict": "pass"}'


def test_handles_empty_string():
    assert _clean_raw('') == ''


def test_strips_multiple_think_blocks():
    result = _clean_raw('<think>a</think><think>b</think>{"verdict": "pass"}')
    assert result == '{"verdict": "pass"}'


def test_whitespace_only():
    assert _clean_raw('  \n  ') == ''


def test_strips_surrounding_whitespace():
    result = _clean_raw('  \n  {"verdict": "pass"}  \n  ')
    assert result == '{"verdict": "pass"}'
