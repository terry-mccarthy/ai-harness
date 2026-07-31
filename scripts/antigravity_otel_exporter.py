#!/usr/bin/env python3
"""
Antigravity Prometheus Log Exporter.

Tails Antigravity CLI transcript logs (~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl)
and exports metrics (token estimates, steps, tool calls, active conversations) via Prometheus HTTP server on port 9011.
"""

import json
import os
import pathlib
import re
import sys
import time
from typing import Dict
from prometheus_client import start_http_server, Counter, Gauge

# --- Prometheus Metrics Definitions ---
TOKENS_TOTAL = Counter(
    "antigravity_tokens_total",
    "Estimated tokens processed by Antigravity CLI",
    ["type", "model"]
)

STEPS_TOTAL = Counter(
    "antigravity_steps_total",
    "Total execution steps in Antigravity transcripts",
    ["step_type", "source", "status"]
)

TOOL_CALLS_TOTAL = Counter(
    "antigravity_tool_calls_total",
    "Total tool calls executed by Antigravity agent",
    ["tool_name"]
)

CONVERSATIONS_TOTAL = Gauge(
    "antigravity_conversations_total",
    "Total active Antigravity conversation threads monitored"
)

LAST_SCRAPE_TIME = Gauge(
    "antigravity_exporter_last_scrape_seconds",
    "Unix timestamp of the last transcript log scan"
)

# File offsets tracking to read only new lines
file_offsets: Dict[str, int] = {}

MODEL_SELECTION_RE = re.compile(r"Model Selection` from .*? to `?([A-Za-z0-9\.\-\_\ ]+?)(?:\.|\`|$)")
PROMPT_STEP_TYPES = {"USER_INPUT", "VIEW_FILE", "GREP_SEARCH", "LIST_DIRECTORY", "RUN_COMMAND", "CHECKPOINT"}
COMPLETION_STEP_TYPES = {"PLANNER_RESPONSE", "MODEL"}

def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 characters per token."""
    if not text:
        return 0
    return max(1, len(text) // 4)

def detect_model(content: str, current_model: str) -> str:
    """Detects a `Model Selection` change announcement in step content."""
    if "Model Selection" not in content:
        return current_model
    match = MODEL_SELECTION_RE.search(content)
    return match.group(1).strip() if match else current_model

def record_step_tokens(step_type: str, content: str, model: str) -> None:
    """Buckets a step's estimated tokens as prompt or completion."""
    if step_type in PROMPT_STEP_TYPES:
        TOKENS_TOTAL.labels(type="prompt", model=model).inc(estimate_tokens(content))
    elif step_type in COMPLETION_STEP_TYPES:
        TOKENS_TOTAL.labels(type="completion", model=model).inc(estimate_tokens(content))

def record_tool_calls(data: dict, model: str) -> None:
    """Tracks tool call counts and their token contribution."""
    tool_calls = data.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for tc in tool_calls:
        TOOL_CALLS_TOTAL.labels(tool_name=tc.get("name", "unknown")).inc()
        TOKENS_TOTAL.labels(type="completion", model=model).inc(estimate_tokens(json.dumps(tc)))

def parse_line(line: str, current_model: str) -> str:
    """Parse a single JSONL line and update metrics. Returns detected model name if any."""
    if not line.strip():
        return current_model

    try:
        data = json.loads(line)
    except Exception:
        return current_model

    step_type = data.get("type", "UNKNOWN")
    content = data.get("content", "")
    current_model = detect_model(content, current_model)

    STEPS_TOTAL.labels(step_type=step_type, source=data.get("source", "UNKNOWN"), status=data.get("status", "DONE")).inc()
    record_step_tokens(step_type, content, current_model)
    record_tool_calls(data, current_model)

    return current_model

def poll_file(filepath: pathlib.Path) -> None:
    """Reads and parses any lines appended to a single transcript file since the last scan."""
    str_path = str(filepath)
    offset = file_offsets.get(str_path, 0)

    file_size = filepath.stat().st_size
    if file_size < offset:
        offset = 0  # file truncated or reset
    if file_size <= offset:
        return

    current_model = "gemini-3.5-flash"
    with open(filepath, "r", encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            current_model = parse_line(line, current_model)
        file_offsets[str_path] = f.tell()

def poll_transcripts():
    """Scans transcript files and processes new lines."""
    brain_dir = pathlib.Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.exists():
        return

    jsonl_files = list(brain_dir.glob("*/.system_generated/logs/transcript.jsonl"))
    CONVERSATIONS_TOTAL.set(len(jsonl_files))

    for filepath in jsonl_files:
        try:
            poll_file(filepath)
        except Exception as e:
            print(f"Error reading {filepath}: {e}", file=sys.stderr)

    LAST_SCRAPE_TIME.set(time.time())

def main():
    port = int(os.environ.get("EXPORTER_PORT", "9011"))
    print(f"Starting Antigravity Prometheus Exporter on http://0.0.0.0:{port}/metrics")
    start_http_server(port)

    # Initial scan
    poll_transcripts()

    # Polling loop
    while True:
        time.sleep(2)
        poll_transcripts()

if __name__ == "__main__":
    main()
