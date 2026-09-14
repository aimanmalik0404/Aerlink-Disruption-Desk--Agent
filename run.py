#!/usr/bin/env python3
"""Entry point: runs one or more disruption-desk cases end to end.

Usage:
    # Full batch over every folder under cases/, with a clean audit trail:
    python run.py --all --reset

    # A single case folder -- this is the "one documented way to hand it a new case"
    # (works for any folder containing inbound.txt, with or without meta.json):
    python run.py --case cases/case-07
    python run.py --case /path/to/some/brand/new/case

    # Override the model:
    python run.py --all --reset --model gpt-4o-mini

Requires OPENAI_API_KEY (and optionally OPS_BASE_URL / OPS_API_KEY) in .env or the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # reads .env in the current directory; harmless no-op if it doesn't exist

from openai import OpenAI

from agent_loop import run_case, DEFAULT_MODEL
from tools import OpsClient

OPS_BASE_URL = os.environ.get("OPS_BASE_URL", "http://127.0.0.1:8642")
OPS_API_KEY = os.environ.get("OPS_API_KEY", "aerlink-ops-local-key")

CASES_DIR = Path("cases")
OUTPUT_DIR = Path("outputs")


def load_case(case_dir: Path) -> tuple[str, str, dict]:
    """Reads inbound.txt (required) and meta.json (optional) from a case folder."""
    inbound_path = case_dir / "inbound.txt"
    if not inbound_path.exists():
        raise FileNotFoundError(f"{case_dir} has no inbound.txt")
    inbound_text = inbound_path.read_text(encoding="utf-8")

    meta_path = case_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    case_id = meta.get("case_id") or case_dir.name
    return case_id, inbound_text, meta


def write_outputs(case_id: str, run) -> None:
    case_out = OUTPUT_DIR / case_id
    case_out.mkdir(parents=True, exist_ok=True)
    (case_out / "record.json").write_text(
        json.dumps(run.record, indent=2, default=str), encoding="utf-8")
    (case_out / "transcript.json").write_text(
        json.dumps(run.transcript, indent=2, default=str), encoding="utf-8")


def run_one(case_dir: Path, model: str, openai_client: OpenAI) -> dict:
    case_id, inbound_text, meta = load_case(case_dir)
    # Fresh client per case -> fresh call_log, so the entitlement guardrail in agent_loop.py
    # never sees another case's tool calls. The server itself still holds shared state.
    ops_client = OpsClient(base_url=OPS_BASE_URL, api_key=OPS_API_KEY)

    print(f"--- running {case_id} ({case_dir}) ---")
    start = time.time()
    run = run_case(case_id, inbound_text, meta, ops_client,
                    openai_client=openai_client, model=model)
    elapsed = time.time() - start

    write_outputs(case_id, run)

    status = (run.record or {}).get("overall_status", "UNKNOWN")
    print(f"    status={status} turns={run.turns_used} tool_calls={run.tool_calls_made} "
          f"est_cost=${run.estimated_cost_usd:.4f} time={elapsed:.1f}s")
    if run.forced_escalation_reason:
        print(f"    ! forced escalation: {run.forced_escalation_reason}")

    return {
        "case_id": case_id,
        "status": status,
        "turns_used": run.turns_used,
        "tool_calls_made": run.tool_calls_made,
        "estimated_cost_usd": run.estimated_cost_usd,
        "elapsed_s": elapsed,
        "forced_escalation_reason": run.forced_escalation_reason,
    }


def main():
    parser = argparse.ArgumentParser(description="Run the Aerlink disruption-desk agent.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true",
                        help="Run every case folder under cases/.")
    group.add_argument("--case", type=str,
                        help="Path to a single case folder (must contain inbound.txt).")
    parser.add_argument("--reset", action="store_true",
                        help="Call POST /_reset on the ops server once before running, "
                             "for a clean audit trail. Recommended for the graded full run.")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help=f"OpenAI model to use (default: {DEFAULT_MODEL}).")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")

    if args.reset:
        reset_client = OpsClient(base_url=OPS_BASE_URL, api_key=OPS_API_KEY)
        result = reset_client.reset()
        print(f"ops server reset: {result}")

    openai_client = OpenAI()
    OUTPUT_DIR.mkdir(exist_ok=True)

    if args.case:
        case_dirs = [Path(args.case)]
    else:
        if not CASES_DIR.exists():
            sys.exit(f"No cases/ directory found at {CASES_DIR.resolve()}")
        case_dirs = sorted(p for p in CASES_DIR.iterdir() if p.is_dir())
        if not case_dirs:
            sys.exit(f"No case folders found under {CASES_DIR.resolve()}")

    summaries = []
    for cd in case_dirs:
        try:
            summaries.append(run_one(cd, args.model, openai_client))
        except Exception as exc:
            print(f"!!! {cd} failed: {exc}")
            summaries.append({"case_id": cd.name, "status": "RUN_FAILED", "error": str(exc)})

    total_cost = sum(s["estimated_cost_usd"] for s in summaries)
    print(f"\n=== {len(summaries)} case(s) complete. Estimated total cost: ${total_cost:.4f} ===")

    (OUTPUT_DIR / "summary.json").write_text(json.dumps({
        "cases": summaries,
        "total_estimated_cost_usd": total_cost,
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()