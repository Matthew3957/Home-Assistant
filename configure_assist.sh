#!/usr/bin/env bash
#
# configure_assist.sh — wire Home Assistant up to local Ollama, post-onboarding.
#
# Run this AFTER you have:
#   1. Started the container with ./bootstrap.sh
#   2. Done initial onboarding in the browser at http://localhost:8123
#   3. Generated a Long-Lived Access Token at /profile/security
#
# What this does:
#   1. Reads (or prompts for) HA_TOKEN, persists to .env.
#   2. Copies overlays/packages/ into homeassistant-config/packages/.
#   3. Appends overlays/configuration_additions.yaml to
#      homeassistant-config/configuration.yaml (idempotent via marker).
#   4. Restarts the HA container so YAML changes take effect.
#   5. Waits for HA to come back.
#   6. Runs ha_api_helper.py inside a local venv to:
#        - add the Ollama integration
#        - create a conversation-agent subentry per model
#        - create one Assist pipeline per model
#        - expose all test entities to Assist
#
# Idempotency:
#   - Re-runs reuse the existing token in .env.
#   - The configuration_additions.yaml block is appended exactly once
#     (gated by the ">>> ollama-testbed" marker line).
#   - The Python helper checks for existing entries / pipelines before
#     creating new ones, and exposing already-exposed entities is a no-op.

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

CONFIG_DIR="./homeassistant-config"
OVERLAYS_DIR="./overlays"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
VENV_DIR=".venv"
MARKER=">>> ollama-testbed: appended by configure_assist.sh"

# ---------------------------------------------------------------------------
# Compose CLI detection (same logic as bootstrap.sh)
# ---------------------------------------------------------------------------
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is installed." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Sanity: HA must already be running. We don't auto-start — that's bootstrap's
# job, and conflating the two scripts hides container failures.
# ---------------------------------------------------------------------------
if ! "${COMPOSE[@]}" ps --format json homeassistant 2>/dev/null | grep -q '"State":"running"'; then
  # Older compose versions don't support --format json — fall back to a
  # cheaper liveness probe.
  if ! curl -sf -o /dev/null --max-time 5 http://localhost:8123 \
       && ! curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:8123 | grep -qE '^(200|401)$'; then
    echo "ERROR: Home Assistant doesn't appear to be running. Run ./bootstrap.sh first." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# .env: create from .env.example on first run, then load and (if needed)
# prompt for the access token.
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Creating $ENV_FILE from $ENV_EXAMPLE ..."
  cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

# Load current .env values into shell. `set -a` makes assignments exported.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [[ -z "${HA_TOKEN:-}" ]]; then
  echo
  echo "Need a Long-Lived Access Token from Home Assistant."
  echo "Generate one at: ${HA_BASE_URL:-http://localhost:8123}/profile/security"
  echo
  # -s: silent (don't echo to terminal). Token is shown only once by HA, so
  # reading it via stdin without echo avoids leaving it in shell history.
  read -r -s -p "Paste token: " TOKEN_INPUT
  echo
  if [[ -z "$TOKEN_INPUT" ]]; then
    echo "ERROR: empty token." >&2
    exit 1
  fi
  # Persist back into .env. We rewrite the HA_TOKEN= line in place to keep
  # any user comments / ordering intact.
  if grep -q '^HA_TOKEN=' "$ENV_FILE"; then
    # macOS sed needs `-i ''`; GNU sed needs `-i`. Use a tempfile to be portable.
    tmp=$(mktemp)
    awk -v tok="$TOKEN_INPUT" '
      /^HA_TOKEN=/ { print "HA_TOKEN=" tok; next }
      { print }
    ' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    echo "HA_TOKEN=$TOKEN_INPUT" >> "$ENV_FILE"
  fi
  export HA_TOKEN="$TOKEN_INPUT"
fi

# Defaults if .env didn't set them.
export HA_BASE_URL="${HA_BASE_URL:-http://localhost:8123}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-gemma4:e4b,qwen3.5:9b,ministral-3:8b}"

# ---------------------------------------------------------------------------
# Overlay copy: packages directory -> live config
# ---------------------------------------------------------------------------
echo "Copying overlays/packages/ into $CONFIG_DIR/packages/ ..."
mkdir -p "$CONFIG_DIR/packages"
# `cp -r` overwrites in place; safe to re-run. We deliberately do NOT delete
# files in the destination so any user-added packages aren't clobbered.
cp -r "$OVERLAYS_DIR/packages/." "$CONFIG_DIR/packages/"

# ---------------------------------------------------------------------------
# Overlay merge: append configuration_additions.yaml to configuration.yaml
# only if our marker line isn't already present.
# ---------------------------------------------------------------------------
CONFIG_YAML="$CONFIG_DIR/configuration.yaml"
if [[ ! -f "$CONFIG_YAML" ]]; then
  echo "ERROR: $CONFIG_YAML doesn't exist. Did you complete the browser onboarding?" >&2
  exit 1
fi

if grep -qF "$MARKER" "$CONFIG_YAML"; then
  echo "configuration.yaml already contains testbed additions — skipping append."
else
  echo "Appending overlays/configuration_additions.yaml to configuration.yaml ..."
  echo "" >> "$CONFIG_YAML"
  cat "$OVERLAYS_DIR/configuration_additions.yaml" >> "$CONFIG_YAML"
fi

# ---------------------------------------------------------------------------
# Restart HA so the new YAML loads. `restart` is preferred over reload
# because we may have introduced new top-level integrations (climate, lock,
# template, media_player) that the YAML reload service doesn't pick up.
# ---------------------------------------------------------------------------
echo "Restarting Home Assistant container ..."
"${COMPOSE[@]}" restart homeassistant

echo "Waiting for HA to come back ..."
for _ in $(seq 1 60); do
  code=$(curl -o /dev/null -s -w "%{http_code}" --max-time 5 "$HA_BASE_URL" || echo "000")
  if [[ "$code" == "200" || "$code" == "401" ]]; then
    echo "HA is back."
    break
  fi
  sleep 2
done

# ---------------------------------------------------------------------------
# Python venv: keep deps isolated from the host Python install.
# ---------------------------------------------------------------------------
if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating Python venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"
echo "Installing Python deps ..."
pip install -q --upgrade pip >/dev/null
pip install -q -r requirements.txt

# ---------------------------------------------------------------------------
# Run the API helper. It does all the integration + pipeline + exposure work
# and prints the resulting pipeline IDs at the end.
# ---------------------------------------------------------------------------
echo
echo "Running ha_api_helper.py ..."
echo
python3 ha_api_helper.py
