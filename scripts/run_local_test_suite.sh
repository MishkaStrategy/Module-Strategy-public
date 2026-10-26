#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_local_test_suite.sh DATASET_DIR [prepare|smoke|full]

Modes:
  prepare  Install the validated local environment, fully scan BTCUSDT 5m,
           generate isolated configs, and run Backtest Engine validate-data.
  smoke    Do prepare, then run smoke twice, compare results, and run replay restart.
  full     Do smoke, then run the full BTCUSDT historical period.
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

DATASET_DIR="$1"
MODE="${2:-prepare}"
case "$MODE" in
  prepare|smoke|full) ;;
  *) usage; exit 2 ;;
esac

CURRENT_SUBSTAGE="bootstrap"
report_failure_substage() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    case "$CURRENT_SUBSTAGE" in
      bootstrap|install_dependencies|local_pretest|smoke_reset|smoke_commands|full_reset|full_backtest) ;;
      *) CURRENT_SUBSTAGE="bootstrap" ;;
    esac
    printf '::error title=Canonical local suite failure::canonical_local_failure mode=%s substage=%s exit_code=%d\n' \
      "$MODE" "$CURRENT_SUBSTAGE" "$status"
  fi
  exit "$status"
}
trap report_failure_substage EXIT

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN=""
for candidate in python3.13 python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.12 or 3.13 is required." >&2
  exit 1
fi

VENV="$ROOT/.venv-local-test"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
PYTHON="$VENV/bin/python"

CURRENT_SUBSTAGE="install_dependencies"
"$PYTHON" -m pip install --upgrade pip setuptools wheel
"$PYTHON" -m pip install -e '.[dev,backtest]'
"$PYTHON" -m pip install -e './modules/session_range[dev]'
"$PYTHON" -m pip install -e ./modules/market_regime
"$PYTHON" -m pip install -e ./plugins/fvg
"$PYTHON" -m pip check

CURRENT_SUBSTAGE="local_pretest"
"$PYTHON" scripts/local_pretest.py \
  --dataset-dir "$DATASET_DIR" \
  --repo-dir "$ROOT" \
  --run-validation

if [[ "$MODE" == "smoke" || "$MODE" == "full" ]]; then
  CURRENT_SUBSTAGE="smoke_reset"
  "$PYTHON" scripts/reset_local_test_results.py \
    --workspace "$ROOT/.local-tests/pretest" \
    smoke smoke-repeat restart-replay
  CURRENT_SUBSTAGE="smoke_commands"
  PYTHON="$PYTHON" "$ROOT/.local-tests/pretest/commands.sh"
fi

if [[ "$MODE" == "full" ]]; then
  CURRENT_SUBSTAGE="full_reset"
  "$PYTHON" scripts/reset_local_test_results.py \
    --workspace "$ROOT/.local-tests/pretest" \
    full-btc
  CURRENT_SUBSTAGE="full_backtest"
  "$PYTHON" -m modular_backtest run \
    --config "$ROOT/.local-tests/pretest/configs/full_btc.yaml" \
    --mode research_fast \
    --max-events 125000000 \
    --max-skip-ranges 500000
fi

cat <<EOF

Completed mode: $MODE
Report: $ROOT/.local-tests/pretest/pretest-report.json
Results: $ROOT/.local-tests/pretest/results
EOF
