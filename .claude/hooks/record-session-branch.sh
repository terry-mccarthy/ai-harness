#!/usr/bin/env bash
# SessionStart hook: records the git branch active when this Claude Code
# session started. OTEL_RESOURCE_ATTRIBUTES (project.branch=...) is read once
# at process startup and never refreshed — see docs/dev/monitoring.md. This
# file lets a later UserPromptSubmit hook detect drift from that snapshot.

STATE_DIR="$CLAUDE_PROJECT_DIR/.claude/.state"
mkdir -p "$STATE_DIR"

git -C "$CLAUDE_PROJECT_DIR" branch --show-current > "$STATE_DIR/session-branch" 2>/dev/null
rm -f "$STATE_DIR/branch-drift-warned"

exit 0
