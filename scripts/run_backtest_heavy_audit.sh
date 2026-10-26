#!/usr/bin/env bash

set -uo pipefail

EXPECTED_SHA="${1:-}"
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "usage: $0 <40-character commit SHA>" >&2
  exit 2
fi
EXPECTED_SHA="$(printf '%s' "$EXPECTED_SHA" | tr 'A-F' 'a-f')"

EVIDENCE_DIR="${EVIDENCE_DIR:-evidence}"
DIST_DIR="${DIST_DIR:-dist}"
HEAVY_VENV="${HEAVY_VENV:-.heavy-venv}"
CLEAN_VENV="${CLEAN_VENV:-.audit-venv}"
mkdir -p "$EVIDENCE_DIR" "$DIST_DIR"
: > "$EVIDENCE_DIR/gates.tsv"
: > "$EVIDENCE_DIR/failures.txt"

AUDIT_PYTHON=""
AUDIT_BIN=""

record_gate() {
  local name="$1"
  local status="$2"
  printf '%s\t%s\n' "$name" "$status" >> "$EVIDENCE_DIR/gates.tsv"
  if [[ "$status" != "success" ]]; then
    printf '%s\n' "$name" >> "$EVIDENCE_DIR/failures.txt"
  fi
}

run_gate() {
  local name="$1"
  shift
  local exit_code
  echo "==> $name"
  (
    set -euo pipefail
    "$@"
  )
  exit_code=$?
  if [[ "$exit_code" -eq 0 ]]; then
    record_gate "$name" "success"
  else
    record_gate "$name" "failure:$exit_code"
  fi
}

write_provenance() {
  mkdir -p "$EVIDENCE_DIR"
  git rev-parse HEAD > "$EVIDENCE_DIR/commit-sha.txt" 2>&1 || true
  uname -a > "$EVIDENCE_DIR/runner.txt" 2>&1 || true
  sw_vers > "$EVIDENCE_DIR/macos-version.txt" 2>&1 || true
  sysctl -n machdep.cpu.brand_string > "$EVIDENCE_DIR/cpu.txt" 2>&1 || true
  sysctl -n hw.memsize > "$EVIDENCE_DIR/memory-bytes.txt" 2>&1 || true
  if [[ -n "$AUDIT_PYTHON" && -x "$AUDIT_PYTHON" ]]; then
    "$AUDIT_PYTHON" --version > "$EVIDENCE_DIR/python-runtime-version.txt" 2>&1 || true
    "$AUDIT_PYTHON" - "$EXPECTED_SHA" "$EVIDENCE_DIR" <<'PY' || true
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

expected_sha = sys.argv[1]
evidence = Path(sys.argv[2])
benchmark_path = evidence / "benchmark-50k-seed1.json"
benchmark = json.loads(benchmark_path.read_text(encoding="utf-8")) if benchmark_path.exists() else {}
payload = {
    "commit_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "expected_sha": expected_sha,
    "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
    "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    "runner_name": os.environ.get("RUNNER_NAME"),
    "runner_os": os.environ.get("RUNNER_OS"),
    "runner_arch": os.environ.get("RUNNER_ARCH"),
    "python": platform.python_version(),
    "configuration_hash": benchmark.get("config_hash"),
    "data_fixture_hash": benchmark.get("data_hash"),
    "module_hash": benchmark.get("module_hash"),
}
(evidence / "environment.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  fi

  local checksum_file="$EVIDENCE_DIR/checksums.sha256"
  : > "$checksum_file"
  while IFS= read -r file; do
    [[ -n "$file" && "$file" != "$checksum_file" ]] || continue
    shasum -a 256 "$file" >> "$checksum_file"
  done < <(find "$EVIDENCE_DIR" -type f ! -path "$checksum_file" -print | LC_ALL=C sort)
}

trap write_provenance EXIT

verify_commit() {
  [[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]]
}

find_python() {
  : > "$EVIDENCE_DIR/python-candidates.txt"
  rm -rf "$HEAVY_VENV"
  local candidate path version audit_python
  for candidate in \
    python3.12 python3.13 python3.14 \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.14 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.13 /usr/local/bin/python3.14 \
    python3; do
    path="$(command -v "$candidate" 2>/dev/null || true)"
    [[ -n "$path" ]] || continue
    version="$("$path" -c 'import platform; print(platform.python_version())' 2>&1 || true)"
    printf '%s -> %s -> %s\n' "$candidate" "$path" "$version" >> "$EVIDENCE_DIR/python-candidates.txt"
    if ! "$path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
      continue
    fi
    if ! "$path" -m venv "$HEAVY_VENV"; then
      rm -rf "$HEAVY_VENV"
      continue
    fi
    audit_python="$PWD/$HEAVY_VENV/bin/python"
    if [[ ! -x "$audit_python" ]]; then
      rm -rf "$HEAVY_VENV"
      continue
    fi
    AUDIT_PYTHON="$audit_python"
    AUDIT_BIN="$PWD/$HEAVY_VENV/bin"
    "$AUDIT_PYTHON" --version | tee "$EVIDENCE_DIR/python-version.txt"
    return 0
  done
  return 1
}

install_environment() {
  "$AUDIT_PYTHON" -m pip install --upgrade pip
  "$AUDIT_PYTHON" -m pip install -e '.[dev,backtest,security,signal-bot]' 'types-psutil>=7.0'
  "$AUDIT_PYTHON" -m pip install -e 'modules/market_data_ingestion[parquet]'
  "$AUDIT_PYTHON" -m pip install -e ./modules/session_range
  "$AUDIT_PYTHON" -m pip install -e ./modules/market_regime
  "$AUDIT_PYTHON" -m pip install -e ./plugins/fvg
  "$AUDIT_PYTHON" -m pip install -e ./modules/setup_adapter
  "$AUDIT_PYTHON" -m pip install -e ./modules/entry_model
  "$AUDIT_PYTHON" -m pip install -e ./modules/protection_target
  "$AUDIT_PYTHON" -m pip install -e ./modules/risk_sizing
  "$AUDIT_PYTHON" -m pip install -e ./modules/position_scaling
  "$AUDIT_PYTHON" -m pip install -e ./modules/trade_plan_router
  "$AUDIT_PYTHON" -m pip install -e './modules/paper_execution[dev]'
  "$AUDIT_PYTHON" -m pip install -e './modules/position_exit_management[dev]'
  "$AUDIT_PYTHON" -m pip install -e './applications/trading_runner[dev]'
  "$AUDIT_PYTHON" -m pip check | tee "$EVIDENCE_DIR/pip-check.txt"
  "$AUDIT_PYTHON" -m pip freeze > "$EVIDENCE_DIR/pip-freeze.txt"
}

compile_sources() {
  "$AUDIT_PYTHON" -m compileall -q src tests benchmarks/backtest_engine.py
  bash -n scripts/run_backtest_heavy_audit.sh
}

ruff_check() {
  "$AUDIT_BIN/ruff" check src/modular_backtest tests/backtest_engine benchmarks/backtest_engine.py \
    > "$EVIDENCE_DIR/ruff-check.txt" 2>&1
}

ruff_format() {
  "$AUDIT_BIN/ruff" format --check src/modular_backtest tests/backtest_engine benchmarks/backtest_engine.py \
    > "$EVIDENCE_DIR/ruff-format.txt" 2>&1
}

strict_mypy() {
  "$AUDIT_BIN/mypy" --strict src/modular_backtest > "$EVIDENCE_DIR/mypy.txt" 2>&1
}

engine_tests() {
  "$AUDIT_BIN/coverage" run --branch --source=src/modular_backtest \
    -m pytest -m "not slow" tests/backtest_engine --junitxml="$EVIDENCE_DIR/backtest-tests.xml"
  "$AUDIT_BIN/coverage" report --fail-under=90 | tee "$EVIDENCE_DIR/backtest-coverage.txt"
  "$AUDIT_BIN/coverage" json -o "$EVIDENCE_DIR/backtest-coverage.json"
}

full_tests() {
  "$AUDIT_PYTHON" -m pytest -m "not slow" --import-mode=importlib \
    --junitxml="$EVIDENCE_DIR/full-tests.xml"
}

recovery_seed_one() {
  PYTHONHASHSEED=1 "$AUDIT_PYTHON" -m pytest \
    tests/backtest_engine/test_recovery.py \
    tests/backtest_engine/test_recovery_subprocess.py \
    --junitxml="$EVIDENCE_DIR/recovery-seed1.xml"
}

recovery_seed_two() {
  PYTHONHASHSEED=9173 "$AUDIT_PYTHON" -m pytest \
    tests/backtest_engine/test_recovery.py \
    tests/backtest_engine/test_recovery_subprocess.py \
    --junitxml="$EVIDENCE_DIR/recovery-seed9173.xml"
}

benchmarks() {
  PYTHONHASHSEED=1 "$AUDIT_PYTHON" benchmarks/backtest_engine.py \
    --bars 50000 --with-restart --output "$EVIDENCE_DIR/benchmark-50k-seed1.json"
  PYTHONHASHSEED=9173 "$AUDIT_PYTHON" benchmarks/backtest_engine.py \
    --bars 50000 --with-restart --output "$EVIDENCE_DIR/benchmark-50k-seed9173.json"
  "$AUDIT_PYTHON" benchmarks/backtest_engine.py \
    --bars 100000 --with-restart --output "$EVIDENCE_DIR/benchmark-100k.json"
  "$AUDIT_PYTHON" benchmarks/backtest_engine.py \
    --bars 250000 --with-restart --output "$EVIDENCE_DIR/benchmark-250k.json"
  "$AUDIT_PYTHON" - "$EVIDENCE_DIR" <<'PY'
import json
import sys
from pathlib import Path

evidence = Path(sys.argv[1])
paths = sorted(evidence.glob("benchmark-*.json"))
rows = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
assert len(rows) == 4
for row in rows:
    assert row["restart_verified"] is True
    assert row["continuous_sha256"] == row["restarted_sha256"]
    assert row["bars_processed"] in {50_000, 100_000, 250_000}
    assert row["elapsed_seconds"] > 0
    assert row["peak_memory_bytes"] > 0
seed1 = json.loads((evidence / "benchmark-50k-seed1.json").read_text(encoding="utf-8"))
seed9173 = json.loads((evidence / "benchmark-50k-seed9173.json").read_text(encoding="utf-8"))
assert seed1["run_id"] == seed9173["run_id"]
assert seed1["continuous_sha256"] == seed9173["continuous_sha256"]
PY
}

wheel_check() {
  rm -rf "$CLEAN_VENV" "$DIST_DIR"
  mkdir -p "$DIST_DIR"
  "$AUDIT_PYTHON" -m pip wheel . --no-deps -w "$DIST_DIR"
  "$AUDIT_PYTHON" -m venv "$CLEAN_VENV"
  "$CLEAN_VENV/bin/python" -m pip install --upgrade pip
  "$CLEAN_VENV/bin/python" -m pip install "$DIST_DIR"/*.whl
  "$CLEAN_VENV/bin/python" -m pip check | tee "$EVIDENCE_DIR/clean-pip-check.txt"
  "$CLEAN_VENV/bin/python" -m modular_backtest --help > "$EVIDENCE_DIR/clean-cli-help.txt"
}

bandit_check() {
  set +e
  "$AUDIT_BIN/bandit" -r src/modular_backtest -f json -o "$EVIDENCE_DIR/bandit.json"
  set -e
  "$AUDIT_PYTHON" - "$EVIDENCE_DIR/bandit.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
recovery = Path("src/modular_backtest/recovery.py").read_text(encoding="utf-8")
verify_at = recovery.find("snapshot.verify()")
load_at = recovery.find("pickle.load(handle)")
verified_local_snapshot = verify_at >= 0 and load_at > verify_at
blocking = []
accepted = []
for finding in payload.get("results", []):
    if finding.get("issue_severity") not in {"MEDIUM", "HIGH"}:
        continue
    is_verified_pickle = (
        finding.get("test_id") == "B301"
        and finding.get("filename") == "src/modular_backtest/recovery.py"
        and verified_local_snapshot
    )
    if is_verified_pickle:
        accepted.append(finding)
    else:
        blocking.append(finding)
Path("evidence/bandit-accepted.json").write_text(
    json.dumps(accepted, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
assert not blocking, f"blocking Bandit findings: {len(blocking)}"
PY
}

secret_check() {
  local files=()
  while IFS= read -r -d '' file; do
    files+=("$file")
  done < <(
    git ls-files -z -- \
      src/modular_backtest \
      tests/backtest_engine \
      benchmarks/backtest_engine.py \
      scripts/run_backtest_heavy_audit.sh \
      configs/backtest.example.yaml
  )
  [[ "${#files[@]}" -gt 0 ]]
  "$AUDIT_BIN/detect-secrets" scan "${files[@]}" > "$EVIDENCE_DIR/detect-secrets.json"
  "$AUDIT_PYTHON" - "$EVIDENCE_DIR/detect-secrets.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert not payload.get("results", {}), "potential secrets detected in Backtest Engine scope"
PY
}

dependency_audit() {
  PIPAPI_PYTHON_LOCATION="$AUDIT_PYTHON" \
    "$AUDIT_BIN/pip-audit" -f json -o "$EVIDENCE_DIR/pip-audit.json"
}

sbom_check() {
  "$AUDIT_BIN/cyclonedx-py" environment \
    --output-format json --output-file "$EVIDENCE_DIR/sbom.json"
}

run_gate verify_commit verify_commit

echo "==> discover_python"
if find_python; then
  record_gate discover_python success
else
  discover_exit=$?
  record_gate discover_python "failure:$discover_exit"
fi

if [[ -n "$AUDIT_PYTHON" && -x "$AUDIT_PYTHON" ]]; then
  run_gate install_environment install_environment
  run_gate compile_sources compile_sources
  run_gate ruff_check ruff_check
  run_gate ruff_format ruff_format
  run_gate strict_mypy strict_mypy
  run_gate engine_tests engine_tests
  run_gate full_tests full_tests
  run_gate recovery_seed1 recovery_seed_one
  run_gate recovery_seed9173 recovery_seed_two
  run_gate benchmarks benchmarks
  run_gate wheel_check wheel_check
  run_gate bandit_check bandit_check
  run_gate secret_check secret_check
  run_gate dependency_audit dependency_audit
  run_gate sbom_check sbom_check
else
  for blocked in \
    install_environment compile_sources ruff_check ruff_format strict_mypy \
    engine_tests full_tests recovery_seed1 recovery_seed9173 benchmarks wheel_check \
    bandit_check secret_check dependency_audit sbom_check; do
    record_gate "$blocked" "blocked:no_python"
  done
fi

if [[ -s "$EVIDENCE_DIR/failures.txt" ]]; then
  echo "Heavy Audit failed gates:" >&2
  cat "$EVIDENCE_DIR/failures.txt" >&2
  exit 1
fi

echo "Heavy Audit completed successfully for $EXPECTED_SHA"
