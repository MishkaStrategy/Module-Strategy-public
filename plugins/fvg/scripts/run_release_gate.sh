#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $0 EXPECTED_SHA CORE_PATH PYTHON_VERSION EVIDENCE_DIR" >&2
  exit 2
fi

EXPECTED_SHA="$1"
CORE_PATH="$2"
PYTHON_VERSION="$3"
EVIDENCE="$4"

if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "EXPECTED_SHA must be a lowercase 40-character commit SHA" >&2
  exit 2
fi

mkdir -p "$EVIDENCE"
actual="$(git rev-parse HEAD)"
core_actual="$(git -C "$CORE_PATH" rev-parse HEAD)"
test "$actual" = "$EXPECTED_SHA"
printf '%s\n' "$actual" > "$EVIDENCE/exact-head.txt"
printf '%s\n' "$core_actual" > "$EVIDENCE/core-head.txt"
printf '%s\n' "$PYTHON_VERSION" > "$EVIDENCE/requested-python.txt"

run_suffix="${GITHUB_RUN_ID:-manual}-${GITHUB_RUN_ATTEMPT:-1}"
runner_temp="${RUNNER_TEMP:-/tmp}"
VENV="$runner_temp/fvg-release-${PYTHON_VERSION}-${run_suffix}"
DIST_ROOT="$runner_temp/fvg-dist-${PYTHON_VERSION}-${run_suffix}"
CLEAN_WHEEL="$runner_temp/fvg-clean-wheel-${PYTHON_VERSION}-${run_suffix}"
CLEAN_SDIST="$runner_temp/fvg-clean-sdist-${PYTHON_VERSION}-${run_suffix}"

cleanup() {
  rm -rf "$VENV" "$DIST_ROOT" "$CLEAN_WHEEL" "$CLEAN_SDIST"
}
trap cleanup EXIT
cleanup
mkdir -p "$DIST_ROOT"

uv python install "$PYTHON_VERSION"
uv venv --python "$PYTHON_VERSION" --seed "$VENV"
"$VENV/bin/python" -m pip install "$CORE_PATH" > "$EVIDENCE/install-core.log" 2>&1
"$VENV/bin/python" -m pip install -e './plugins/fvg[dev]' > "$EVIDENCE/install-fvg.log" 2>&1
"$VENV/bin/python" -m pip install \
  'setuptools>=68' wheel build twine bandit detect-secrets pip-audit cyclonedx-bom \
  > "$EVIDENCE/install-release-tools.log" 2>&1
"$VENV/bin/python" -m pip check > "$EVIDENCE/pip-check.log" 2>&1
"$VENV/bin/python" --version > "$EVIDENCE/python-version.txt" 2>&1
"$VENV/bin/python" -m pip freeze --all > "$EVIDENCE/toolchain-freeze.txt"

OUTCOMES="$EVIDENCE/outcomes.txt"
: > "$OUTCOMES"
failures=0

run_gate() {
  local name="$1"
  shift
  local log="$EVIDENCE/${name}.log"
  set +e
  (
    set -euo pipefail
    "$@"
  ) > "$log" 2>&1
  local status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    printf '%s=success\n' "$name" | tee -a "$OUTCOMES"
  else
    printf '%s=failure\n' "$name" | tee -a "$OUTCOMES"
    failures=1
  fi
}

gate_canonicalize() {
  "$VENV/bin/ruff" format \
    plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
  "$VENV/bin/ruff" check --fix \
    plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
  git diff --check
  git diff -- plugins/fvg > "$EVIDENCE/canonicalization.patch"
}

gate_source_clean() {
  git diff --exit-code -- plugins/fvg
}

gate_compileall() {
  "$VENV/bin/python" -m compileall -q \
    plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
}

gate_ruff_format() {
  "$VENV/bin/ruff" format --check \
    plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
}

gate_ruff_lint() {
  "$VENV/bin/ruff" check \
    plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
}

gate_mypy() {
  "$VENV/bin/mypy" --strict plugins/fvg/src/modular_strategy_fvg
}

gate_runtime_tests() {
  "$VENV/bin/python" -m pytest -q plugins/fvg/tests/unit/test_runtime_v030.py \
    --junitxml="$EVIDENCE/runtime-tests.xml"
}

gate_full_tests() {
  "$VENV/bin/python" -m pytest -q plugins/fvg/tests \
    --junitxml="$EVIDENCE/full-tests.xml"
}

gate_coverage() {
  export COVERAGE_FILE="$EVIDENCE/.coverage"
  "$VENV/bin/coverage" erase
  "$VENV/bin/coverage" run --branch --source=modular_strategy_fvg \
    -m pytest -q plugins/fvg/tests
  "$VENV/bin/coverage" report --show-missing --fail-under=95 \
    > "$EVIDENCE/coverage-report.txt"
  "$VENV/bin/coverage" json -o "$EVIDENCE/coverage.json"
}

gate_optimized_tests() {
  "$VENV/bin/python" -O -m pytest -q plugins/fvg/tests
}

gate_benchmark() {
  "$VENV/bin/python" plugins/fvg/benchmarks/run_benchmarks.py --ci \
    --output "$EVIDENCE/benchmark.json"
  "$VENV/bin/python" - "$EVIDENCE/benchmark.json" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report.get("module_version") != "0.3.0":
    raise SystemExit("benchmark used an unexpected FVG version")
restart = report.get("restart_midpoint")
if not isinstance(restart, dict) or restart.get("digests_match") is not True:
    raise SystemExit("restart benchmark digest mismatch")
memory = report.get("memory")
if not isinstance(memory, dict) or not isinstance(memory.get("peak_memory_bytes"), int):
    raise SystemExit("benchmark memory evidence is missing")
extended = report.get("extended")
throughput = extended.get("throughput_calls_per_second") if isinstance(extended, dict) else None
if not isinstance(throughput, dict) or float(throughput.get("minimum", 0)) <= 0:
    raise SystemExit("benchmark throughput evidence is invalid")
PY
}

gate_packaging() {
  local epoch
  epoch="$(git show -s --format=%ct HEAD)"
  mkdir -p "$DIST_ROOT"/{raw1,raw2,norm1,norm2}
  for build_index in 1 2; do
    rm -rf plugins/fvg/dist plugins/fvg/build plugins/fvg/src/*.egg-info
    SOURCE_DATE_EPOCH="$epoch" PYTHONHASHSEED="$build_index" \
      "$VENV/bin/python" -m build --outdir "$DIST_ROOT/raw$build_index" plugins/fvg \
      > "$EVIDENCE/build-$build_index.log" 2>&1
    local raw_wheel raw_sdist
    raw_wheel="$(find "$DIST_ROOT/raw$build_index" -maxdepth 1 -name '*.whl' -print -quit)"
    raw_sdist="$(find "$DIST_ROOT/raw$build_index" -maxdepth 1 -name '*.tar.gz' -print -quit)"
    test -n "$raw_wheel" && test -n "$raw_sdist"
    "$VENV/bin/python" plugins/fvg/scripts/normalize_wheel.py --epoch "$epoch" \
      "$raw_wheel" "$DIST_ROOT/norm$build_index/modular_strategy_fvg-0.3.0-py3-none-any.whl"
    "$VENV/bin/python" plugins/fvg/scripts/normalize_sdist.py --epoch "$epoch" \
      "$raw_sdist" "$DIST_ROOT/norm$build_index/modular_strategy_fvg-0.3.0.tar.gz"
  done
  cmp "$DIST_ROOT/norm1/modular_strategy_fvg-0.3.0-py3-none-any.whl" \
    "$DIST_ROOT/norm2/modular_strategy_fvg-0.3.0-py3-none-any.whl"
  cmp "$DIST_ROOT/norm1/modular_strategy_fvg-0.3.0.tar.gz" \
    "$DIST_ROOT/norm2/modular_strategy_fvg-0.3.0.tar.gz"
  "$VENV/bin/twine" check "$DIST_ROOT/norm1"/* > "$EVIDENCE/twine-check.txt"
  "$VENV/bin/python" -m zipfile -t \
    "$DIST_ROOT/norm1/modular_strategy_fvg-0.3.0-py3-none-any.whl" \
    > "$EVIDENCE/wheel-archive-check.txt"
  tar -tzf "$DIST_ROOT/norm1/modular_strategy_fvg-0.3.0.tar.gz" \
    > "$EVIDENCE/sdist-members.txt"
  mkdir -p "$EVIDENCE/distributions"
  cp "$DIST_ROOT/norm1"/* "$EVIDENCE/distributions/"
  sha256sum "$DIST_ROOT/norm1"/* > "$EVIDENCE/distribution-checksums.sha256"
}

smoke_install() {
  local python_executable="$1"
  "$python_executable" - <<'PY'
from importlib.metadata import entry_points, version
from importlib.resources import files
from modular_strategy_fvg import FvgModule

assert version("modular-strategy-fvg") == "0.3.0"
assert files("modular_strategy_fvg").joinpath("py.typed").is_file()
eps = {
    ep.name: ep
    for ep in entry_points().select(group="modular_strategy.modules")
}
assert eps["fvg"].load() is FvgModule
state = FvgModule(timeframes=("15m",)).snapshot_state()
restored = FvgModule(timeframes=("15m",))
restored.restore_state(state)
assert restored.snapshot_state() == state
PY
}

gate_clean_install() {
  local wheel="$DIST_ROOT/norm1/modular_strategy_fvg-0.3.0-py3-none-any.whl"
  local sdist="$DIST_ROOT/norm1/modular_strategy_fvg-0.3.0.tar.gz"
  rm -rf "$CLEAN_WHEEL" "$CLEAN_SDIST"
  uv venv --python "$PYTHON_VERSION" --seed "$CLEAN_WHEEL"
  "$CLEAN_WHEEL/bin/python" -m pip install "$CORE_PATH"
  "$CLEAN_WHEEL/bin/python" -m pip install "$wheel"
  "$CLEAN_WHEEL/bin/python" -m pip check
  smoke_install "$CLEAN_WHEEL/bin/python"

  uv venv --python "$PYTHON_VERSION" --seed "$CLEAN_SDIST"
  "$CLEAN_SDIST/bin/python" -m pip install "$CORE_PATH"
  "$CLEAN_SDIST/bin/python" -m pip install "$sdist"
  "$CLEAN_SDIST/bin/python" -m pip check
  smoke_install "$CLEAN_SDIST/bin/python"
}

gate_bandit() {
  "$VENV/bin/bandit" -q -r plugins/fvg/src plugins/fvg/scripts \
    -f json -o "$EVIDENCE/bandit.json"
}

gate_secrets() {
  "$VENV/bin/detect-secrets" scan --all-files --no-verify \
    plugins/fvg/src plugins/fvg/scripts \
    plugins/fvg/pyproject.toml plugins/fvg/setup.cfg \
    .github/workflows/ci.yml .github/workflows/fvg-module-ci.yml \
    .github/workflows/fvg-release-validation.yml \
    > "$EVIDENCE/detect-secrets.json"
  "$VENV/bin/python" - "$EVIDENCE/detect-secrets.json" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
findings = report.get("results")
if not isinstance(findings, dict):
    raise SystemExit("detect-secrets report is missing a results mapping")
non_empty = {path: rows for path, rows in findings.items() if rows}
if non_empty:
    raise SystemExit(f"detect-secrets findings require review: {sorted(non_empty)}")
PY
}

gate_pip_audit() {
  local site_path
  site_path="$($CLEAN_WHEEL/bin/python - <<'PY'
import site
print(site.getsitepackages()[0])
PY
)"
  "$VENV/bin/pip-audit" --path "$site_path" --format json \
    --output "$EVIDENCE/pip-audit.json" --progress-spinner off
}

gate_sbom() {
  "$VENV/bin/cyclonedx-py" environment "$CLEAN_WHEEL/bin/python" \
    --output-reproducible --spec-version 1.6 --output-format JSON \
    --output-file "$EVIDENCE/sbom.cdx.json"
}

run_gate canonicalize gate_canonicalize
run_gate source_clean gate_source_clean
run_gate compileall gate_compileall
run_gate ruff_format gate_ruff_format
run_gate ruff_lint gate_ruff_lint
run_gate mypy gate_mypy
run_gate runtime_tests gate_runtime_tests
run_gate full_tests gate_full_tests
run_gate coverage gate_coverage
run_gate optimized_tests gate_optimized_tests
run_gate benchmark gate_benchmark
run_gate packaging gate_packaging
run_gate clean_install gate_clean_install
run_gate bandit gate_bandit
run_gate secrets gate_secrets
run_gate pip_audit gate_pip_audit
run_gate sbom gate_sbom

line_count="$(wc -l < "$OUTCOMES")"
success_count="$(grep -c '=success$' "$OUTCOMES" || true)"
printf 'gates=%s\nsuccesses=%s\n' "$line_count" "$success_count" > "$EVIDENCE/summary.txt"

test "$line_count" -eq 17
test "$success_count" -eq 17
test "$failures" -eq 0
