#!/usr/bin/env python3
"""
ha_api_helper.py — invoked by configure_assist.sh.

Drives the Home Assistant REST + WebSocket APIs to:
  1. Add the Ollama integration pointing at OLLAMA_BASE_URL.
  2. Create one Assist conversation-agent subentry per model in OLLAMA_MODELS.
  3. Create one Assist pipeline per model.
  4. Expose every test entity to Assist.
  5. Print the resulting pipeline IDs (so test_pipeline.py can use them).

Idempotency contract:
  * If Ollama is already configured at this URL, we reuse the entry.
  * If a pipeline with the target name already exists, we update instead of
    creating a duplicate.
  * Re-exposing an already-exposed entity is a no-op in HA.

API endpoints / commands used (call out for future maintenance if HA changes):
  REST  POST /api/config/config_entries/flow                  - start flow
  REST  POST /api/config/config_entries/flow/{flow_id}        - submit step
  REST  GET  /api/config/config_entries/entry                 - list entries
  WS    config_entries/subentries/list                        - list subentries
  WS    config_entries/subentries/flow                        - start subflow
  WS    assist_pipeline/pipeline/list                         - list pipelines
  WS    assist_pipeline/pipeline/create                       - create pipeline
  WS    assist_pipeline/pipeline/update                       - update pipeline
  WS    homeassistant/expose_entity                           - expose to Assist
  WS    conversation/agent/list                               - find agents

If a future HA release renames any of these, search for the literal command
strings below and adjust.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

import requests
import websockets


# ---------------------------------------------------------------------------
# Config — all read from the environment, populated by configure_assist.sh
# ---------------------------------------------------------------------------
HA_BASE_URL = os.environ["HA_BASE_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"].rstrip("/")
OLLAMA_MODELS = [m.strip() for m in os.environ["OLLAMA_MODELS"].split(",") if m.strip()]

# Default system prompt fed to every model. Kept simple per spec — this is a
# testbed, not a production assistant.
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful home assistant for a two-person household. "
    "You can control lights, locks, thermostats, switches, and media players, "
    "and read sensors. Be concise. When the user asks you to do something, "
    "do it via tool calls and then confirm in one short sentence. "
    "Never invent entities or values that aren't in your tool surface."
)

# Test entity domains we want exposed to Assist. Anything in these domains
# gets exposed to all conversation agents. Adjust if you add new domains.
EXPOSE_DOMAINS = (
    "light",
    "lock",
    "climate",
    "input_boolean",
    "sensor",
    "binary_sensor",
    "media_player",
    "input_text",
    "input_datetime",
)


def ws_url() -> str:
    """Convert HA_BASE_URL (http(s)://...) into a ws(s) URL ending /api/websocket."""
    parsed = urlparse(HA_BASE_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/api/websocket"


def rest_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


# ===========================================================================
# REST helpers — used for config-flow setup of the Ollama integration
# ===========================================================================
def list_config_entries() -> list[dict[str, Any]]:
    """All config entries currently registered with HA."""
    r = requests.get(
        f"{HA_BASE_URL}/api/config/config_entries/entry",
        headers=rest_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def find_ollama_entry() -> dict[str, Any] | None:
    """Return an existing Ollama config entry pointing at OLLAMA_BASE_URL, if any."""
    for entry in list_config_entries():
        if entry.get("domain") != "ollama":
            continue
        # Newer HA exposes `data` in the entry list; older versions don't,
        # in which case we just match on domain.
        data = entry.get("data") or {}
        if not data or data.get("url") == OLLAMA_BASE_URL:
            return entry
    return None


def start_config_flow(handler: str) -> dict[str, Any]:
    """Initiate a config flow for the given integration handler."""
    r = requests.post(
        f"{HA_BASE_URL}/api/config/config_entries/flow",
        headers=rest_headers(),
        json={"handler": handler, "show_advanced_options": False},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def submit_config_flow_step(flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Submit form data for the current step of an in-progress config flow."""
    r = requests.post(
        f"{HA_BASE_URL}/api/config/config_entries/flow/{flow_id}",
        headers=rest_headers(),
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def add_ollama_integration() -> str:
    """Add the Ollama integration if missing. Returns the config entry id."""
    existing = find_ollama_entry()
    if existing:
        print(f"  [skip] Ollama integration already present (entry_id={existing['entry_id']}).")
        return existing["entry_id"]

    print(f"  [api]  Starting Ollama config flow against {OLLAMA_BASE_URL} ...")
    flow = start_config_flow("ollama")

    # The Ollama config flow's first step is `user` and asks for the URL.
    # Subsequent steps may auto-create the entry if a model is also requested
    # in the same payload, depending on HA version. We submit the URL only
    # and let HA finish via its abort/create_entry path.
    while flow.get("type") == "form":
        step_id = flow.get("step_id", "user")
        if step_id == "user":
            flow = submit_config_flow_step(flow["flow_id"], {"url": OLLAMA_BASE_URL})
        else:
            # Some versions ask for a model in the same flow. We send an
            # empty / first model — it'll be reconfigured per-subentry below.
            flow = submit_config_flow_step(
                flow["flow_id"],
                {"model": OLLAMA_MODELS[0]},
            )

    if flow.get("type") == "create_entry":
        entry_id = flow["result"]["entry_id"]
        print(f"  [ok]   Created Ollama entry {entry_id}.")
        return entry_id
    if flow.get("type") == "abort":
        # `single_instance_allowed` or `already_configured` aborts are fine —
        # find the entry that exists.
        existing = find_ollama_entry()
        if existing:
            print(f"  [ok]   Flow aborted '{flow.get('reason')}', reusing {existing['entry_id']}.")
            return existing["entry_id"]
        raise RuntimeError(f"Ollama flow aborted: {flow.get('reason')}")
    raise RuntimeError(f"Unexpected Ollama flow result: {flow}")


# ===========================================================================
# WebSocket plumbing
# ===========================================================================
class HAWebSocket:
    """Thin async wrapper around HA's websocket API with auto-incrementing ids."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 1

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one command, await the matching `result` message, return it."""
        msg_id = self._id
        self._id += 1
        full = {"id": msg_id, **payload}
        await self.ws.send(json.dumps(full))
        # The server may interleave `event` messages — skip them until we
        # see `type: result` with our id.
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id and msg.get("type") == "result":
                return msg


async def authenticate(ws) -> None:
    """Complete HA's auth handshake. Raises if the token is invalid."""
    hello = json.loads(await ws.recv())
    if hello.get("type") != "auth_required":
        raise RuntimeError(f"Unexpected hello: {hello}")
    await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
    reply = json.loads(await ws.recv())
    if reply.get("type") != "auth_ok":
        raise RuntimeError(f"Auth failed: {reply}")


# ===========================================================================
# Subentries (per-model conversation agents)
# ===========================================================================
async def list_subentries(ws_helper: HAWebSocket, entry_id: str) -> list[dict[str, Any]]:
    """List subentries belonging to a parent config entry."""
    res = await ws_helper.send({
        "type": "config_entries/subentries/list",
        "entry_id": entry_id,
    })
    if not res.get("success"):
        # Older HA versions may not have subentries — treat as empty.
        print(f"  [warn] subentries/list failed ({res.get('error')}); assuming none.")
        return []
    return res.get("result", [])


async def add_conversation_subentry(
    ws_helper: HAWebSocket, entry_id: str, model: str
) -> str | None:
    """
    Add a `conversation` subentry to the Ollama config entry for the given
    model. Returns the subentry id, or None if the API call failed (we'll
    fall back to a printed instruction).
    """
    # Start the subentry flow.
    init = await ws_helper.send({
        "type": "config_entries/subentries/flow",
        "handler": ["ollama", "conversation"],
        "entry_id": entry_id,
    })
    if not init.get("success"):
        print(f"    [warn] subentries/flow init failed for {model}: {init.get('error')}")
        return None

    flow = init["result"]
    # Walk the form steps. The Ollama conversation subentry typically asks
    # for: model, prompt, max_history, num_ctx, "control HA" toggle.
    while flow.get("type") == "form":
        step_payload = {
            "name": f"Ollama ({model})",
            "model": model,
            "prompt": DEFAULT_SYSTEM_PROMPT,
            "llm_hass_api": "assist",  # enables HA tool-calling
            # Sensible defaults; adjust in the UI if you want different ones.
            "max_history": 20,
            "num_ctx": 8192,
        }
        step = await ws_helper.send({
            "type": "config_entries/subentries/flow/step",
            "flow_id": flow["flow_id"],
            "user_input": step_payload,
        })
        if not step.get("success"):
            print(f"    [warn] subentry step failed for {model}: {step.get('error')}")
            return None
        flow = step["result"]

    if flow.get("type") == "create_entry":
        sub_id = flow.get("result", {}).get("subentry_id") or flow.get("subentry_id")
        print(f"    [ok]   Subentry for {model} = {sub_id}")
        return sub_id
    if flow.get("type") == "abort":
        print(f"    [skip] subentry for {model} aborted ({flow.get('reason')}).")
        return None
    print(f"    [warn] Unexpected subentry flow result for {model}: {flow}")
    return None


# ===========================================================================
# Pipelines
# ===========================================================================
async def list_pipelines(ws_helper: HAWebSocket) -> list[dict[str, Any]]:
    res = await ws_helper.send({"type": "assist_pipeline/pipeline/list"})
    if not res.get("success"):
        raise RuntimeError(f"pipeline/list failed: {res.get('error')}")
    return res.get("result", {}).get("pipelines", [])


async def list_conversation_agents(ws_helper: HAWebSocket) -> list[dict[str, Any]]:
    """All conversation agents currently registered (Ollama subentries appear here)."""
    res = await ws_helper.send({"type": "conversation/agent/list"})
    if not res.get("success"):
        return []
    return res.get("result", {}).get("agents", [])


async def upsert_pipeline(
    ws_helper: HAWebSocket,
    name: str,
    conversation_engine: str,
) -> str:
    """
    Create or update an Assist pipeline named `name` whose conversation agent
    is `conversation_engine`. Other slots (STT/TTS/wake-word) are left at
    HA defaults — this testbed is text-only.
    """
    pipelines = await list_pipelines(ws_helper)
    existing = next((p for p in pipelines if p.get("name") == name), None)

    body = {
        "name": name,
        "language": "en",
        "conversation_engine": conversation_engine,
        "conversation_language": "en",
        # Text-only: leave STT, TTS, wake_word as None.
        "stt_engine": None,
        "stt_language": None,
        "tts_engine": None,
        "tts_language": None,
        "tts_voice": None,
        "wake_word_entity": None,
        "wake_word_id": None,
    }

    if existing:
        res = await ws_helper.send({
            "type": "assist_pipeline/pipeline/update",
            "pipeline_id": existing["id"],
            **body,
        })
        if not res.get("success"):
            raise RuntimeError(f"pipeline/update failed for {name}: {res.get('error')}")
        print(f"    [ok]   Updated pipeline '{name}' (id={existing['id']}).")
        return existing["id"]

    res = await ws_helper.send({
        "type": "assist_pipeline/pipeline/create",
        **body,
    })
    if not res.get("success"):
        raise RuntimeError(f"pipeline/create failed for {name}: {res.get('error')}")
    pid = res["result"]["id"]
    print(f"    [ok]   Created pipeline '{name}' (id={pid}).")
    return pid


# ===========================================================================
# Entity exposure
# ===========================================================================
async def expose_entities(ws_helper: HAWebSocket) -> int:
    """
    Expose every entity in EXPOSE_DOMAINS to Assist (assistant='conversation').
    Returns the number of entities exposed.
    """
    states = requests.get(
        f"{HA_BASE_URL}/api/states", headers=rest_headers(), timeout=15
    ).json()
    targets = [
        s["entity_id"]
        for s in states
        if s["entity_id"].split(".", 1)[0] in EXPOSE_DOMAINS
    ]

    if not targets:
        print("  [warn] No matching entities found to expose. Did the package YAML load?")
        return 0

    res = await ws_helper.send({
        "type": "homeassistant/expose_entity",
        "assistants": ["conversation"],
        "entity_ids": targets,
        "should_expose": True,
    })
    if not res.get("success"):
        # Non-fatal — exposure can be done in the UI.
        print(f"  [warn] expose_entity failed: {res.get('error')}")
        return 0
    return len(targets)


# ===========================================================================
# Main
# ===========================================================================
async def main() -> int:
    print(f"Target HA: {HA_BASE_URL}")
    print(f"Target Ollama: {OLLAMA_BASE_URL}")
    print(f"Models: {', '.join(OLLAMA_MODELS)}")
    print()

    print("[1/4] Adding Ollama integration ...")
    entry_id = add_ollama_integration()

    async with websockets.connect(ws_url(), max_size=4 * 1024 * 1024) as ws:
        await authenticate(ws)
        helper = HAWebSocket(ws)

        # --- 2. Subentry (conversation agent) per model ---------------------
        print("[2/4] Adding a conversation-agent subentry per model ...")
        existing_subs = await list_subentries(helper, entry_id)
        existing_models = {
            (s.get("data") or {}).get("model"): s for s in existing_subs
        }
        for model in OLLAMA_MODELS:
            if model in existing_models:
                print(f"    [skip] Subentry for {model} already exists.")
            else:
                await add_conversation_subentry(helper, entry_id, model)

        # --- 3. Pipeline per model ------------------------------------------
        print("[3/4] Creating one Assist pipeline per model ...")
        # Find the conversation_engine string for each model. The agent.list
        # API returns entries like {"id": "conversation.ollama_<model>", ...}.
        agents = await list_conversation_agents(helper)
        # Map model name -> agent id heuristically. Fall back to a guess.
        def agent_for(model: str) -> str:
            # Try to find an agent whose id mentions the model substring.
            tag = model.split(":")[0].replace("-", "_").replace(".", "_")
            for a in agents:
                aid = a.get("id", "")
                if tag in aid:
                    return aid
            # Fallback to the canonical pattern. Update HA UI manually if wrong.
            return f"conversation.ollama_{tag}"

        pipeline_ids: dict[str, str] = {}
        for model in OLLAMA_MODELS:
            pid = await upsert_pipeline(
                helper,
                name=f"Ollama {model}",
                conversation_engine=agent_for(model),
            )
            pipeline_ids[model] = pid

        # --- 4. Expose entities ---------------------------------------------
        print("[4/4] Exposing test entities to Assist ...")
        n = await expose_entities(helper)
        print(f"    [ok]   Exposed {n} entities to the conversation assistant.")

    print()
    print("=" * 78)
    print("Pipeline IDs (use with test_pipeline.py --pipeline <id>):")
    for model, pid in pipeline_ids.items():
        print(f"  {model:>20s}  ->  {pid}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
