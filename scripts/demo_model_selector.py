"""Demo: automatic model selection by task complexity conserves tokens.

The selector (built separately) assigns each SRE task a complexity tier.
This script runs the same batch twice — once with tier-matched models and
once with a single baseline model — then prints a side-by-side comparison.

Config file (JSON, path via SELECTOR_CONFIG env var or --config flag):

    {
      "llm_provider": "openrouter",
      "openrouter": {"api_key": "sk-or-..."},
      "role_models": {
        "low":    {"provider": "openrouter", "model": "anthropic/claude-haiku-4-5-20251001"},
        "medium": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4-6"},
        "high":   {"provider": "openrouter", "model": "anthropic/claude-opus-4-8"}
      },
      "baseline_model": "anthropic/claude-opus-4-8",
      "pricing": {
        "anthropic/claude-haiku-4-5-20251001": {"prompt": 0.80,  "completion": 4.00},
        "anthropic/claude-sonnet-4-6":         {"prompt": 3.00,  "completion": 15.00},
        "anthropic/claude-opus-4-8":           {"prompt": 15.00, "completion": 75.00}
      }
    }

Usage:
    make demo-model-selector
    SELECTOR_CONFIG=config.json python scripts/demo_model_selector.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from measure_selector import TokenLedger, comparison_report

from harness_agents.dynamic_sre import DynamicSREAgent
from harness_agents.llm import build_llm_from_env, build_role_llm
from harness_agents.types import AgentState
from harness_gateway.client import GatewayClient

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Task batch — tiers pre-assigned by the external selector
# ---------------------------------------------------------------------------
TASK_BATCH: list[tuple[str, str, str]] = [
    # (name, tier, prompt)
    (
        "escalation path",
        "low",
        "What is the on-call escalation path for database incidents?",
    ),
    (
        "maintenance window policy",
        "low",
        "What is the standard maintenance window policy for production services?",
    ),
    (
        "API 503 spike",
        "medium",
        (
            "API gateway logs show elevated 503 error rates over the last 15 minutes. "
            "Identify the upstream service causing the errors and the likely reason."
        ),
    ),
    (
        "worker-3 memory climb",
        "medium",
        (
            "Memory usage on worker-3 has been climbing for 2 hours — now at 94%. "
            "Find the root process and assess whether a restart is needed."
        ),
    ),
    (
        "architect token loop",
        "high",
        (
            "Grafana cost dashboard shows the architect agent role consuming tokens "
            "at 4x the normal rate for the past 30 minutes. Two threads appear stuck "
            "in a loop with no final_response produced."
        ),
    ),
    (
        "checkout latency spike",
        "high",
        (
            "Production checkout service latency jumped from 50 ms p99 to 8 s at 14:03. "
            "Revenue impact is ongoing. Diagnose the root cause and recommend immediate steps."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(path: str | None) -> dict:
    cfg_path = path or os.environ.get("SELECTOR_CONFIG")
    if cfg_path:
        return json.loads(Path(cfg_path).read_text())
    return {}


def _make_gateway() -> GatewayClient:
    return GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )


_RETRY_DELAY_RE = re.compile(r"retry in (\d+\.?\d*)s", re.IGNORECASE)
_MAX_RETRIES = 3


async def _run_task(
    gateway: GatewayClient,
    llm_provider,
    task_prompt: str,
) -> dict:
    agent = DynamicSREAgent(gateway=gateway, llm_provider=llm_provider)
    state: AgentState = {
        "task": task_prompt,
        "thread_id": str(uuid.uuid4()),
        "diff": "",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }
    for attempt in range(_MAX_RETRIES):
        result = await agent.run(state)
        err = result.get("error") or {}
        if err.get("code") == "provider_error" and "429" in str(err.get("reason", "")):
            m = _RETRY_DELAY_RE.search(str(err["reason"]))
            delay = float(m.group(1)) if m else 20.0
            print(f"    rate-limited — waiting {delay:.0f}s (attempt {attempt + 1}/{_MAX_RETRIES})", flush=True)
            await asyncio.sleep(delay)
            continue
        return result
    return result


async def _run_pass(
    gateway: GatewayClient,
    config: dict,
    use_baseline: bool,
    baseline_model: str,
) -> TokenLedger:
    label = "baseline" if use_baseline else "selected"
    ledger = TokenLedger(label)

    for name, tier, prompt in TASK_BATCH:
        if use_baseline:
            llm = build_llm_from_env(
                config={
                    **config,
                    config.get("llm_provider", "openrouter"): {
                        **config.get(config.get("llm_provider", "openrouter"), {}),
                        "model": baseline_model,
                    },
                }
            )
            model_name = baseline_model
        else:
            llm = build_role_llm(tier, config)
            model_name = llm.model_name

        print(f"  [{label}] {name:30s} tier={tier:6s} model={model_name.split('/')[-1]}", flush=True)
        result = await _run_task(gateway, llm, prompt)
        task_delay = config.get("task_delay_seconds", 2)
        await asyncio.sleep(task_delay)

        usage = result.get("token_usage") or {}
        ledger.record(
            name=name,
            tier=tier,
            model=model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        if result.get("error"):
            print(f"    ! error: {result['error']}", flush=True)

    return ledger


async def main(config_path: str | None = None) -> None:
    config = _load_config(config_path)
    pricing = config.get("pricing")
    baseline_model = config.get(
        "baseline_model",
        config.get(config.get("llm_provider", "openrouter"), {}).get("model", "anthropic/claude-opus-4-8"),
    )

    gateway = _make_gateway()

    print("\n=== SELECTED PASS (tier-matched models) ===")
    selected = await _run_pass(gateway, config, use_baseline=False, baseline_model=baseline_model)

    print("\n=== BASELINE PASS (single model for all tasks) ===")
    baseline = await _run_pass(gateway, config, use_baseline=True, baseline_model=baseline_model)

    print("\n")
    print(comparison_report(selected, baseline, pricing=pricing))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Model-selector token comparison demo")
    parser.add_argument("--config", default=None, help="Path to selector config JSON")
    args = parser.parse_args()
    asyncio.run(main(args.config))
