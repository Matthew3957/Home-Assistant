#!/usr/bin/env bash
#
# bootstrap.sh — bring up Home Assistant in Docker for the Ollama testbed.
#
# Idempotent. Re-run after edits to docker-compose.yml or after a reboot.
# Does NOT touch ./homeassistant-config beyond creating it the first time —
# all your onboarding state, secrets, and DB are preserved across runs.
#
# What this script does:
#   1. Ensures ./homeassistant-config exists (HA's persistent volume).
#   2. Brings the container up via docker compose (or docker-compose).
#   3. Waits for HA to answer on http://localhost:8123 (up to 2 min).
#   4. Prints next-step onboarding instructions.

set -euo pipefail

# Run from the script's directory so relative paths in docker-compose.yml work
# regardless of where the user invoked the script from.
cd "$(dirname "$(readlink -f "$0")")"

CONFIG_DIR="./homeassistant-config"
HA_URL="http://localhost:8123"
TIMEOUT_SECONDS=120

# ---------------------------------------------------------------------------
# Pick whichever docker compose CLI is present. Modern installs have the
# `docker compose` plugin; older systems still have the standalone binary.
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
# Create the persistent config directory if missing. mkdir -p is idempotent.
# We deliberately do NOT chown — HA's container handles ownership internally.
# ---------------------------------------------------------------------------
if [[ ! -d "$CONFIG_DIR" ]]; then
  echo "Creating $CONFIG_DIR ..."
  mkdir -p "$CONFIG_DIR"
else
  echo "$CONFIG_DIR already exists — leaving contents alone."
fi

# ---------------------------------------------------------------------------
# Bring the stack up. `up -d` is idempotent: if the container is already
# running with the current config, it's a no-op. If docker-compose.yml has
# changed, the container is recreated.
# ---------------------------------------------------------------------------
echo "Starting Home Assistant container..."
"${COMPOSE[@]}" up -d

# ---------------------------------------------------------------------------
# Poll the HA HTTP endpoint until it responds. HA's first-boot can take
# 30–90s while it builds the venv, generates default config, and starts the
# integration manager. We tolerate connection-refused (HA not listening yet)
# and any 2xx/3xx/401 response (401 means HA is up but unauthenticated,
# which is exactly the "ready for onboarding" state we want).
# ---------------------------------------------------------------------------
echo "Waiting for Home Assistant to become reachable at $HA_URL ..."
elapsed=0
while (( elapsed < TIMEOUT_SECONDS )); do
  # -o /dev/null: discard body. -s: silent. -w "%{http_code}": just the code.
  # || true: curl exits non-zero on connection refused; we handle that below.
  code=$(curl -o /dev/null -s -w "%{http_code}" --max-time 5 "$HA_URL" || echo "000")
  if [[ "$code" =~ ^(2|3|401)..?$ ]] || [[ "$code" == "200" ]] || [[ "$code" == "401" ]]; then
    echo "Home Assistant is up (HTTP $code)."
    break
  fi
  sleep 2
  elapsed=$((elapsed + 2))
  printf "."
done
echo ""

if (( elapsed >= TIMEOUT_SECONDS )); then
  echo "ERROR: Home Assistant did not respond within ${TIMEOUT_SECONDS}s." >&2
  echo "Check container logs with:  ${COMPOSE[*]} logs -f homeassistant" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Onboarding instructions. Initial setup (account, location, units) is a
# manual browser step — scripting it via the REST API is fragile and breaks
# between HA releases.
# ---------------------------------------------------------------------------
cat <<EOF

================================================================================
Home Assistant is running.

Next steps (manual, in your browser):

  1. Open $HA_URL
  2. Create your admin account.
  3. Set location, units, and timezone (timezone should already be PT).
  4. On the "Devices" prompt, click "Finish" — we'll add Ollama via script.
  5. Go to your profile -> Security tab -> "Long-Lived Access Tokens"
     -> "Create Token". Name it something like "configure-assist".
     Copy the token (you only see it once).
  6. Run:  ./configure_assist.sh
     and paste the token when prompted.

================================================================================
EOF
