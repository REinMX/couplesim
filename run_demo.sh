#!/usr/bin/env bash
# Standalone demo of the coupled master/slave simulation, no ERT needed.
# Runs the same forward-model driver that ERT executes per realization.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 ert/bin/scripts/run_coupled.py --demo "$@"
