#!/usr/bin/env bash
# Turnkey local runner for the roster health monitor.
#
#   ./run_local.sh --dry-run     # print the digest, send nothing (start here)
#   ./run_local.sh               # real run: notify on change, persist state
#
# It sources ./.env (copy .env.example -> .env first), makes sure deps are
# installed, then runs the monitor against config.yaml. Any extra args are
# passed straight through to monitor.py.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a; . ./.env; set +a
else
  echo "note: no .env found (copy .env.example -> .env and fill it in)." >&2
fi

# Install deps once; quiet if already satisfied.
python3 -m pip install -q -r requirements.txt

exec python3 monitor.py --config config.yaml "$@"
