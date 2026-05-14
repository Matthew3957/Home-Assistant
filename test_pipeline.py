#!/usr/bin/env python3
"""
test_pipeline.py — minimal smoke test for an Assist pipeline.

Sends one prompt to the Home Assistant /api/conversation/process REST
endpoint, prints the spoken response, and surfaces any tool calls the
conversation agent made.

Designed to be a stub: the full bake-off harness (multiple prompts,
latency measurement, golden checks) will extend this. Keep additions
small and composable.

Usage:
    python test_pipeline.py --pipeline <pipeline_id> [--prompt "..."]

If --pipeline is omitted, HA's default pipeline is used. Get pipeline
IDs from the tail of `configure_assist.sh` output, or look them up in
Settings -> Voice assistants in the UI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import requests


def load_env(path: Path) -> dict[str, str]:
    """Tiny .env loader — no python-dotenv dep, since we own the format."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt",
        default="What's the temperature outside?",
        help="Prompt to send (default: %(default)r).",
    )
    parser.add_argument(
        "--pipeline",
        default=None,
        help="Pipeline ID. If omitted, HA's default pipeline is used.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Conversation language (default: en).",
    )
    args = parser.parse_args()

    env_path = Path(__file__).parent / ".env"
    env = load_env(env_path)
    # Allow real environment to override .env, useful for ad-hoc CLI invocation.
    base_url = (os.environ.get("HA_BASE_URL") or env.get("HA_BASE_URL", "")).rstrip("/")
    token = os.environ.get("HA_TOKEN") or env.get("HA_TOKEN", "")

    if not base_url or not token:
        print("ERROR: HA_BASE_URL and HA_TOKEN must be set (in .env or env).", file=sys.stderr)
        return 1

    payload: dict[str, object] = {
        "text": args.prompt,
        "language": args.language,
        # conversation_id keeps multi-turn context; a fresh UUID per run gives
        # us a clean slate. Pass --pipeline to override the agent.
        "conversation_id": str(uuid.uuid4()),
    }
    if args.pipeline:
        # The conversation/process endpoint accepts an `agent_id` field that
        # routes to a specific conversation agent. For pipelines specifically,
        # pass the pipeline_id; HA resolves it to the configured agent.
        payload["agent_id"] = args.pipeline

    print(f"-> {base_url}/api/conversation/process")
    print(f"-> prompt: {args.prompt!r}")
    if args.pipeline:
        print(f"-> pipeline: {args.pipeline}")
    print()

    r = requests.post(
        f"{base_url}/api/conversation/process",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,  # local LLMs can be slow on first token
    )
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
        return 2

    data = r.json()

    # --- Spoken response ---------------------------------------------------
    speech = (
        data.get("response", {})
        .get("speech", {})
        .get("plain", {})
        .get("speech")
    )
    print("Response:")
    print(f"  {speech or '(no speech text)'}")
    print()

    # --- Targets / tool calls ---------------------------------------------
    # HA's intent system returns the entities it acted on under
    # response.data.targets / response.data.success / response.data.failed.
    targets = data.get("response", {}).get("data", {})
    if targets:
        success = targets.get("success") or []
        failed = targets.get("failed") or []
        if success:
            print("Acted on:")
            for t in success:
                print(f"  + {t.get('id') or t.get('name')}")
        if failed:
            print("Failed targets:")
            for t in failed:
                print(f"  - {t.get('id') or t.get('name')}: {t.get('error')}")

    # --- Full payload, for debugging --------------------------------------
    print()
    print("Full response (debug):")
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
