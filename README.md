# Home-Assist

Local Home Assistant testbed for evaluating small LLMs (Gemma, Qwen, Ministral)
as the conversation agent through HA's Assist pipeline. Everything runs on
the host: HA in Docker, Ollama on the metal, no cloud dependencies.

## Why this exists

The goal is a clean, reproducible bake-off harness. We spin up a vanilla HA
with a realistic 2-person household entity surface (lights, locks, climate,
sensors, media), wire it to a local Ollama daemon, and create one Assist
pipeline per model. From there, `test_pipeline.py` (and future bake-off
scripts) pose prompts and we compare responses + tool calls across models.

## Prerequisites

- Linux host (verified on kernel 6+)
- Docker with the `compose` plugin (or standalone `docker-compose`)
- Ollama running locally on `:11434` with the test models pulled:
  ```
  ollama list
  # gemma4:e4b      ...
  # qwen3.5:9b      ...
  # ministral-3:8b  ...
  ```
  If your tags differ, edit `OLLAMA_MODELS` in `.env` after step 4.
- Python 3.10+ (for the API helper and `test_pipeline.py`)
- ~6 GB free disk for HA's container image + persistent state

> **VRAM note.** This was scoped against an RTX 5060 (8 GB). The 8B/9B
> models will pressure that budget — expect significant CPU offload at
> default Ollama quantization. Use Q4_K_M quants if you want full GPU
> residency.

## Setup walkthrough

### 1. Bring up Home Assistant

```
./bootstrap.sh
```

Creates `homeassistant-config/`, starts the container, polls until HA
answers, and prints onboarding instructions. Idempotent — safe to re-run.

### 2. Onboard in the browser

Open http://localhost:8123 and:
1. Create your admin account.
2. Set location, units, timezone (should already be PT from `TZ` env var).
3. On the "Devices" page click **Finish** — we add Ollama via script.

### 3. Generate a Long-Lived Access Token

In HA, click your profile (bottom-left) → **Security** tab →
**Long-Lived Access Tokens** → **Create Token**. Name it
`configure-assist`. **Copy it now — HA shows it once.**

### 4. Wire up Ollama and Assist pipelines

```
./configure_assist.sh
```

You'll be prompted for the token; it gets stored in `.env` for re-runs.
The script will:
- Copy the entity overlays into the live config
- Append to `configuration.yaml` (once, marker-gated)
- Restart the container so the YAML loads
- Set up a Python venv in `.venv/`
- Add the Ollama integration
- Create a conversation-agent subentry per model
- Create one Assist pipeline per model
- Expose the test entities to Assist

The last thing it prints is a table of pipeline IDs — copy these.

### 5. Smoke-test a pipeline

```
.venv/bin/python test_pipeline.py --pipeline <pipeline_id> \
    --prompt "Turn on the kitchen pendant and tell me the outdoor temperature."
```

The script prints the spoken response, any entities the model acted on,
and the full JSON payload for debugging.

## Swapping or adding models

Edit `OLLAMA_MODELS` in `.env` (comma-separated tags, must already be
pulled by Ollama), then re-run `./configure_assist.sh`. The helper is
idempotent, so existing pipelines/subentries are reused; new ones are
added.

You can also do it manually in the UI:
- **Settings → Devices & services → Ollama → Add subentry** for each
  new model
- **Settings → Voice assistants → Add Assistant** to create the pipeline,
  then point its conversation agent at the new Ollama subentry

## Repo layout

```
.
├── README.md                          this file
├── docker-compose.yml                 HA in host network mode
├── bootstrap.sh                       start container, wait, print next steps
├── configure_assist.sh                post-onboarding wiring (idempotent)
├── ha_api_helper.py                   API/WebSocket worker invoked by ^^
├── test_pipeline.py                   single-prompt smoke test
├── requirements.txt                   websockets + requests
├── .env.example                       template (copy to .env)
├── .gitignore                         hides .env, homeassistant-config, venv
├── overlays/
│   ├── configuration_additions.yaml   appended to configuration.yaml
│   └── packages/
│       ├── evening_routine.yaml       dining room switch, Apple TV, evening automations
│       ├── test_devices.yaml          lights, climate, locks, switches, etc.
│       └── test_helpers.yaml          shopping list + upcoming events
└── homeassistant-config/              [gitignored] HA persistent state
```

## Troubleshooting

### Ollama "connection refused" from inside HA
HA runs in host network mode, so `localhost:11434` inside the container
points at the host. Verify Ollama is bound to all interfaces (or at least
`127.0.0.1`):
```
curl -s http://localhost:11434/api/tags | head
```
If you ever switch HA to bridge networking, change `OLLAMA_BASE_URL` to
`http://host.docker.internal:11434` in `.env` and add `extra_hosts:
- "host.docker.internal:host-gateway"` to `docker-compose.yml`.

### Device discovery (mDNS / SSDP) doesn't see anything
You're almost certainly not on host network mode. Confirm with:
```
docker inspect homeassistant | grep -i NetworkMode
# expected: "NetworkMode": "host"
```

### Port 8123 already in use
Another HA instance, Pi-hole admin, etc. Either stop the other process
or remap. Remapping requires bridge networking (see device-discovery
caveat above) — easier to free the port.

### "Configuration check failed" after configure_assist.sh
Open Developer Tools → YAML in HA and click **Check Configuration**.
Most often this is a duplicate key (e.g. you also have a top-level
`light:` block in `configuration.yaml`) — packages can't merge across
files for some integration domains. Move conflicting blocks into the
package.

### Pipelines created but model errors out at runtime
Check the container logs for the model name HA is actually requesting:
```
docker logs -f homeassistant 2>&1 | grep -i ollama
```
Mismatch between `OLLAMA_MODELS` in `.env` and what's in `ollama list`
is the usual culprit. Edit `.env`, re-run `./configure_assist.sh`.

### "Token invalid" from ha_api_helper.py
Long-lived tokens are tied to the user that created them. If you rebuilt
the container or wiped `homeassistant-config/`, regenerate. To force a
re-prompt, delete the `HA_TOKEN=` line from `.env` and re-run.

## Cleaning up

```
docker compose down              # stop the container
rm -rf homeassistant-config/     # blow away all HA state (irreversible)
rm -rf .venv .env                # remove local Python env + token
```
