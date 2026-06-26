#!/usr/bin/env bash
# Pre-push gate: runs code review and architectural diff review in parallel.
# Blocks the push if either review finds CRITICAL or HIGH severity findings.
# If the review server is unreachable, warns and allows the push to proceed.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')

# Only intercept git push commands
if ! echo "$COMMAND" | grep -qE '\bgit push\b'; then
  exit 0
fi

if [[ "${SKIP_ARCH_REVIEW:-0}" == "1" ]]; then
  echo "Pre-push review: skipped (SKIP_ARCH_REVIEW=1)." >&2
  exit 0
fi

echo "Pre-push review: checking diff before push..." >&2

# Write diff to temp file
DIFF_FILE=$(mktemp)
trap 'rm -f "$DIFF_FILE"' EXIT

MERGE_BASE=$(git merge-base HEAD main 2>/dev/null || echo "main")
git diff "$MERGE_BASE" HEAD > "$DIFF_FILE" 2>/dev/null || true

if [ ! -s "$DIFF_FILE" ]; then
  echo "Pre-push review: no diff vs main — skipping." >&2
  exit 0
fi

REVIEW_SERVER="${REVIEW_SERVER_URL:-http://localhost:9003}"

# Check server availability (5s timeout)
if ! curl -sf --max-time 5 "$REVIEW_SERVER/metrics" >/dev/null 2>&1; then
  echo "WARNING: Review server not reachable at $REVIEW_SERVER — proceeding without review." >&2
  exit 0
fi

# Run code review + architectural diff review in parallel, collect blockers
python3 - "$DIFF_FILE" "$REVIEW_SERVER" <<'PYEOF'
import json, sys, urllib.request, urllib.error, subprocess, re
from concurrent.futures import ThreadPoolExecutor

diff_file, server = sys.argv[1], sys.argv[2]

with open(diff_file) as f:
    diff = f.read()


def _post(url, payload, timeout=180):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get_github_url():
    try:
        raw = subprocess.check_output(["git", "remote", "get-url", "origin"], stderr=subprocess.DEVNULL).decode().strip()
        # Normalise SSH → HTTPS
        ssh = re.match(r"git@github\.com:(.+?)(?:\.git)?$", raw)
        if ssh:
            return f"https://github.com/{ssh.group(1)}"
        https = re.match(r"(https://github\.com/[^/]+/[^/]+?)(?:\.git)?$", raw)
        if https:
            return https.group(1)
    except Exception:
        pass
    return None


def run_code_review():
    try:
        result = _post(f"{server}/review", {"diff_text": diff})
        findings = result.get("findings", [])
        blockers = [f for f in findings if f.get("severity") in ("CRITICAL", "HIGH")]
        return "code", blockers, None
    except Exception as e:
        return "code", [], str(e)


def run_arch_review(repo_url):
    try:
        result = _post(
            f"{server}/review-architecture",
            {"target_mode": "diff", "diff": diff, "repo": repo_url},
            timeout=300,
        )
        findings = result.get("findings", [])
        blockers = [f for f in findings if f.get("severity") in ("CRITICAL", "HIGH")]
        return "arch", blockers, None
    except Exception as e:
        return "arch", [], str(e)


repo_url = get_github_url()

with ThreadPoolExecutor(max_workers=2) as ex:
    futures = [ex.submit(run_code_review)]
    if repo_url:
        futures.append(ex.submit(run_arch_review, repo_url))
    else:
        print("WARNING: No GitHub remote found — skipping architectural review.", file=sys.stderr)
    results = [f.result() for f in futures]

all_blockers = {}
for label, blockers, err in results:
    name = "Code review" if label == "code" else "Architectural review"
    if err:
        print(f"  {name}: WARNING — failed ({err}), skipped.", file=sys.stderr)
    elif blockers:
        print(f"  {name}: BLOCKED ({len(blockers)} critical/high finding(s))", file=sys.stderr)
        all_blockers[name] = blockers
    else:
        print(f"  {name}: passed", file=sys.stderr)

if all_blockers:
    print("BLOCKED: Pre-push review found issues that must be resolved:", file=sys.stderr)
    for review_name, blockers in all_blockers.items():
        print(f"\n  [{review_name}]", file=sys.stderr)
        for f in blockers:
            sev = f.get("severity", "?")
            title = f.get("title", "(no title)")
            msg = f.get("message", "")
            loc = f.get("location", "")
            print(f"    [{sev}] {title}", file=sys.stderr)
            if msg:
                print(f"           {msg}", file=sys.stderr)
            if loc:
                print(f"           @ {loc}", file=sys.stderr)
    print("\nFix the issues above before pushing, or set SKIP_ARCH_REVIEW=1 to override.", file=sys.stderr)
    sys.exit(2)

total_info = sum(1 for _, blockers, _ in results if not blockers and _ is None for _ in blockers)
print("Pre-push review passed.", file=sys.stderr)
sys.exit(0)
PYEOF
