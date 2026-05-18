#!/bin/sh
# Wrapper invoked by cron to refresh app/data/rtt_measured.json.
#
# Expects to live at /opt/llm-price-runner/tools/cron_probes.sh on the VPS,
# with a Python virtualenv at /opt/llm-price-runner/.venv-probes/ containing
# httpx and python-dotenv. RIPE_ATLAS_KEY is read from .env (loaded by
# the script itself via python-dotenv). Logs go to /var/log/probes.log.
#
# Cron entry (root crontab, weekly Sunday 03:00 Europe/Vilnius):
#   0 3 * * 0 /opt/llm-price-runner/tools/cron_probes.sh

set -eu
PROJECT_ROOT="${PROJECT_ROOT:-/opt/llm-price-runner}"
VENV="${VENV:-${PROJECT_ROOT}/.venv-probes}"
LOG="${LOG:-/var/log/probes.log}"

cd "${PROJECT_ROOT}"
{
  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) starting probe refresh ====="
  "${VENV}/bin/python" -m tools.run_probes
  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) done ====="
} >> "${LOG}" 2>&1
