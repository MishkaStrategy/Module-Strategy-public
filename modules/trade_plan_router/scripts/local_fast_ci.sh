#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd -- "$MODULE_DIR/../.." && pwd)"
WORKSPACE_DIR="$(cd -- "$REPO_DIR/.." && pwd)"
REPORT_DIR="${REPORT_DIR:-$REPO_DIR/reports/trade_plan_router}"
CORE_PROJECT="${CORE_PROJECT:-$WORKSPACE_DIR/sources/modular_strategy_core-0.4.0}"
CORE_SRC="${CORE_SRC:-$CORE_PROJECT/src}"
ENTRY_SRC="${ENTRY_SRC:-$WORKSPACE_DIR/sources/modular_strategy_entry_model-0.2.0/src}"
PROTECTION_SRC="${PROTECTION_SRC:-$WORKSPACE_DIR/sources/modular_strategy_protection_target-1.1.1/src}"
RISK_SRC="${RISK_SRC:-$WORKSPACE_DIR/sources/modular_strategy_risk_sizing-1.2.0/src}"
SCALING_SRC="${SCALING_SRC:-$WORKSPACE_DIR/sources/modular_strategy_position_scaling-0.2.2/src}"
PYYAML_WHEEL="${PYYAML_WHEEL:-$WORKSPACE_DIR/build-round-16/deps/PyYAML-6.0.3-cp313-cp313-manylinux_2_17_x86_64.whl}"
PYTHON_BIN="${PYTHON_BIN:-python}"
PYTHONPATH_VALUE="$CORE_SRC:$ENTRY_SRC:$PROTECTION_SRC:$RISK_SRC:$SCALING_SRC:$MODULE_DIR/src"
ROUND="${1:-local-fast-ci}"
RUNTIME_DIR="$REPORT_DIR/runtime/$ROUND"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-315532800}"

mkdir -p "$REPORT_DIR"
rm -rf "$RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR"
cd "$MODULE_DIR"

{
  printf 'python=%s\n' "$($PYTHON_BIN --version 2>&1)"
  printf 'module_dir=%s\n' "$MODULE_DIR"
  printf 'repo_dir=%s\n' "$REPO_DIR"
  printf 'core_project=%s\n' "$CORE_PROJECT"
  printf 'core_src=%s\n' "$CORE_SRC"
  printf 'entry_src=%s\n' "$ENTRY_SRC"
  printf 'protection_src=%s\n' "$PROTECTION_SRC"
  printf 'risk_src=%s\n' "$RISK_SRC"
  printf 'scaling_src=%s\n' "$SCALING_SRC"
  printf 'source_date_epoch=%s\n' "$SOURCE_DATE_EPOCH"
} | tee "$REPORT_DIR/environment-$ROUND.txt"

for required in "$CORE_PROJECT/pyproject.toml" "$CORE_SRC" "$MODULE_DIR/pyproject.toml"; do
  if [[ ! -e "$required" ]]; then
    printf 'missing required local source: %s\n' "$required" >&2
    exit 2
  fi
done

"$PYTHON_BIN" scripts/check_path_collisions.py "$MODULE_DIR" \
  | tee "$REPORT_DIR/path-collisions-$ROUND.txt"

PYTHONPATH="$PYTHONPATH_VALUE" "$PYTHON_BIN" -m compileall -q src tests scripts benchmarks

PYTHONPATH="$PYTHONPATH_VALUE" "$PYTHON_BIN" - <<'PY' \
  | tee "$REPORT_DIR/package-metadata-$ROUND.txt"
from pathlib import Path
import tomllib

from modular_strategy_trade_plan_router import TradePlanRouterModule

root = Path.cwd()
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
project_version = project["project"]["version"]
assert project_version == TradePlanRouterModule.manifest.version
assert (root / "src/modular_strategy_trade_plan_router/py.typed").is_file()
print("source_package_metadata: PASS")
print(f"project_version={project_version}")
PY

if git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$REPO_DIR" diff --check | tee "$REPORT_DIR/diff-check-$ROUND.txt"
else
  "$PYTHON_BIN" scripts/check_text_files.py "$MODULE_DIR" \
    | tee "$REPORT_DIR/diff-check-$ROUND.txt"
fi

PYTHONPATH="$PYTHONPATH_VALUE" "$PYTHON_BIN" -m pytest -q --tb=short \
  | tee "$REPORT_DIR/tests-$ROUND.txt"

rm -f .coverage
PYTHONPATH="$PYTHONPATH_VALUE" "$PYTHON_BIN" -m coverage run --branch \
  --source=src/modular_strategy_trade_plan_router -m pytest -q
"$PYTHON_BIN" -m coverage report --fail-under=90 -m \
  | tee "$REPORT_DIR/coverage-$ROUND.txt"

PYTHONPATH="$PYTHONPATH_VALUE" "$PYTHON_BIN" scripts/security_scan.py \
  | tee "$REPORT_DIR/static-security-$ROUND.txt"

PYTHONPATH="$PYTHONPATH_VALUE" "$PYTHON_BIN" scripts/type_surface_audit.py \
  | tee "$REPORT_DIR/type-surface-$ROUND.txt"

"$PYTHON_BIN" scripts/verify_golden_matrix.py \
  | tee "$REPORT_DIR/golden-matrix-$ROUND.txt"

replay_dir="$RUNTIME_DIR/replay"
mkdir -p "$replay_dir"
for spec in 'UTC:1' 'Europe/Amsterdam:9173' 'Asia/Bangkok:99991' 'America/New_York:31337'; do
  tz="${spec%%:*}"
  seed="${spec##*:}"
  TZ="$tz" PYTHONHASHSEED="$seed" PYTHONPATH="$PYTHONPATH_VALUE" \
    "$PYTHON_BIN" scripts/deterministic_replay.py > "$replay_dir/$seed.json"
done
cmp "$replay_dir/1.json" "$replay_dir/9173.json"
cmp "$replay_dir/1.json" "$replay_dir/99991.json"
cmp "$replay_dir/1.json" "$replay_dir/31337.json"
cp "$replay_dir/1.json" "$REPORT_DIR/deterministic-replay-$ROUND.json"
printf 'deterministic_replay: PASS\n' | tee "$REPORT_DIR/deterministic-replay-$ROUND.txt"

# Fully local wheel build. No index or build isolation is permitted here.
wheel_root="$RUNTIME_DIR/wheels"
mkdir -p "$wheel_root/first" "$wheel_root/second"
for output in "$wheel_root/first" "$wheel_root/second"; do
  PIP_NO_INDEX=1 SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    "$PYTHON_BIN" -m pip wheel --no-index --no-build-isolation --no-deps \
    "$CORE_PROJECT" -w "$output" >/dev/null
  PIP_NO_INDEX=1 SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    "$PYTHON_BIN" -m pip wheel --no-index --no-build-isolation --no-deps \
    "$MODULE_DIR" -w "$output" >/dev/null
done

first_core="$(find "$wheel_root/first" -maxdepth 1 -name 'modular_strategy_core-*.whl' -print -quit)"
first_router="$(find "$wheel_root/first" -maxdepth 1 -name 'modular_strategy_trade_plan_router-*.whl' -print -quit)"
second_core="$(find "$wheel_root/second" -maxdepth 1 -name 'modular_strategy_core-*.whl' -print -quit)"
second_router="$(find "$wheel_root/second" -maxdepth 1 -name 'modular_strategy_trade_plan_router-*.whl' -print -quit)"
for wheel_file in "$first_core" "$first_router" "$second_core" "$second_router"; do
  [[ -f "$wheel_file" ]] || { printf 'expected wheel missing\n' >&2; exit 3; }
done
sha256sum "$first_core" "$first_router" "$second_core" "$second_router" \
  | tee "$REPORT_DIR/wheel-hashes-$ROUND.sha256"
[[ "$(sha256sum "$first_core" | awk '{print $1}')" == "$(sha256sum "$second_core" | awk '{print $1}')" ]]
[[ "$(sha256sum "$first_router" | awk '{print $1}')" == "$(sha256sum "$second_router" | awk '{print $1}')" ]]
printf 'reproducible_wheels: PASS\n' | tee "$REPORT_DIR/reproducible-wheels-$ROUND.txt"
"$PYTHON_BIN" -m zipfile -l "$first_router" | tee "$REPORT_DIR/wheel-contents-$ROUND.txt"
"$PYTHON_BIN" - "$first_router" <<'PY' \
  | tee "$REPORT_DIR/wheel-policy-$ROUND.txt"
from pathlib import Path
import sys
from zipfile import ZipFile

wheel = Path(sys.argv[1])
with ZipFile(wheel) as archive:
    names = tuple(archive.namelist())
assert "modular_strategy_trade_plan_router/py.typed" in names
for forbidden in ("tests/", "reports/", ".coverage", ".pytest_cache", "__pycache__"):
    assert not any(forbidden in name for name in names), forbidden
assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)
print("wheel_content_policy: PASS")
print(f"wheel_entries={len(names)}")
PY

if [[ ! -f "$PYYAML_WHEEL" ]]; then
  printf 'PyYAML local wheel unavailable: %s\n' "$PYYAML_WHEEL" \
    | tee "$REPORT_DIR/wheel-install-$ROUND.txt"
  exit 4
fi
venv_dir="$RUNTIME_DIR/venv"
"$PYTHON_BIN" -m venv "$venv_dir"
PIP_NO_INDEX=1 "$venv_dir/bin/python" -m pip install --no-index --no-deps \
  "$PYYAML_WHEEL" "$first_core" "$first_router" \
  | tee "$REPORT_DIR/wheel-install-$ROUND.txt"
"$venv_dir/bin/python" -m pip check | tee "$REPORT_DIR/pip-check-$ROUND.txt"
(
  cd "$RUNTIME_DIR"
  PYTHONPATH= "$venv_dir/bin/python" - <<'PY'
from importlib.metadata import entry_points, version
from modular_strategy_trade_plan_router import TradePlanRouterModule

module = TradePlanRouterModule()
assert module.manifest.name == "trade_plan_router"
assert module.snapshot_state() == {}
points = [
    item
    for item in entry_points(group="modular_strategy.modules")
    if item.name == "trade_plan_router"
]
assert len(points) == 1
assert points[0].load() is TradePlanRouterModule
print("clean_wheel_import: PASS")
print(f"router_version={version('modular-strategy-trade-plan-router')}")
print(f"core_version={version('modular-strategy-core')}")
PY
) | tee "$REPORT_DIR/wheel-import-$ROUND.txt"

PIP_NO_INDEX=1 "$venv_dir/bin/python" -m pip uninstall -y \
  modular-strategy-trade-plan-router \
  | tee "$REPORT_DIR/wheel-uninstall-$ROUND.txt"
(
  cd "$RUNTIME_DIR"
  PYTHONPATH= "$venv_dir/bin/python" - <<'PY'
from importlib.metadata import entry_points
from importlib.util import find_spec

assert find_spec("modular_strategy_trade_plan_router") is None
assert not [
    item
    for item in entry_points(group="modular_strategy.modules")
    if item.name == "trade_plan_router"
]
print("clean_wheel_uninstall: PASS")
PY
) | tee -a "$REPORT_DIR/wheel-uninstall-$ROUND.txt"

PIP_NO_INDEX=1 "$venv_dir/bin/python" -m pip install --no-index --no-deps \
  "$first_router" \
  | tee "$REPORT_DIR/wheel-reinstall-$ROUND.txt"
(
  cd "$RUNTIME_DIR"
  PYTHONPATH= "$venv_dir/bin/python" - <<'PY'
from importlib.metadata import entry_points
from modular_strategy_trade_plan_router import (
    TradePlanRouterModule,
    TradePlanSerializer,
)

module = TradePlanRouterModule()
serializer = TradePlanSerializer()
assert module.manifest.version == "0.1.0"
assert serializer.max_payload_bytes > 0
points = [
    item
    for item in entry_points(group="modular_strategy.modules")
    if item.name == "trade_plan_router"
]
assert len(points) == 1
assert points[0].load() is TradePlanRouterModule
print("clean_wheel_reinstall: PASS")
PY
) | tee -a "$REPORT_DIR/wheel-reinstall-$ROUND.txt"
"$venv_dir/bin/python" -m pip check | tee "$REPORT_DIR/pip-check-reinstall-$ROUND.txt"

# Record optional-tool availability without claiming unavailable checks passed.
{
  for tool in ruff mypy pyright basedpyright bandit pip-audit mutmut; do
    if command -v "$tool" >/dev/null 2>&1; then
      printf '%s=AVAILABLE\n' "$tool"
    else
      printf '%s=BLOCKED_TOOL_UNAVAILABLE\n' "$tool"
    fi
  done
} | tee "$REPORT_DIR/tool-availability-$ROUND.txt"

printf 'local_fast_ci: PASS_WITH_EXPLICIT_OPTIONAL_TOOLING_BLOCKERS\n' \
  | tee "$REPORT_DIR/result-$ROUND.txt"
