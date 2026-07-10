#!/usr/bin/env bash
# Hunt rebuild verification suite (resequenced plan)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=(uv run python)

"${PY[@]}" -m compileall -q hunt_core
"${PY[@]}" -m hunt_core._dev.budget || true
"${PY[@]}" -m hunt_core._dev.check_imports
"${PY[@]}" -m hunt_core._dev.check_ccxt
"${PY[@]}" -m hunt_core._dev.check_config
"${PY[@]}" -m hunt_core._dev.check_lake_schema
"${PY[@]}" -m hunt_core._dev.check_logic || true
"${PY[@]}" -m hunt_core._dev.factor_promotion_gate
"${PY[@]}" -m hunt_core._dev.check_quarantine_factors || true
"${PY[@]}" -m hunt_core._dev.quarantine_oos_report || true
"${PY[@]}" -m hunt_core._dev.lake_soak_status || true
"${PY[@]}" -m hunt_core._dev.replay_fusion --all --walk-forward 0.3 || true
"${PY[@]}" -m hunt_core._dev.authority_audit || true
"${PY[@]}" -m hunt_core._dev.check_deep
"${PY[@]}" -m hunt_core._dev.check_analyst
"${PY[@]}" -m hunt_core._dev.check_deep_e2e
"${PY[@]}" -m hunt_core._dev.check_plan_complete
"${PY[@]}" -m hunt_core._dev.check_phase9
HUNT_LIVE=1 "${PY[@]}" -m hunt_core._dev.check_deep_e2e --live
echo "verify_hunt_rebuild: done"
