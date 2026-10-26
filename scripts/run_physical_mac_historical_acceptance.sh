#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly CANONICAL_REL="BTCUSDT/BTCUSDT_5m.parquet"
readonly BASELINE_SHA="3962fe5116c05ca9858bc9bcaf03a6e0d84c9e66"

: "${EXPECTED_HEAD:?EXPECTED_HEAD is required}"
: "${EXPECTED_BASE:?EXPECTED_BASE is required}"
: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

RUN_ROOT="$RUNNER_TEMP/module-strategy-physical-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
PUBLISH_EVIDENCE_DIR="${PHYSICAL_EVIDENCE_DIR:-$GITHUB_WORKSPACE/.physical-acceptance/historical}"
PUBLISH_FAILURE_DIR="$PUBLISH_EVIDENCE_DIR/failure"
EVIDENCE_DIR="$RUN_ROOT/evidence/historical"
PRIVATE_ROOT="$RUN_ROOT/private"
CORPUS_FILE="$PRIVATE_ROOT/corpus-root.txt"
DATASET_VIEW="$PRIVATE_ROOT/dataset-view"
PRIVATE_LOG_DIR="$PRIVATE_ROOT/logs"
CURRENT_STAGE="bootstrap"

[ "$EVIDENCE_DIR" != "$PUBLISH_EVIDENCE_DIR" ] || {
  printf '[ERROR] private evidence and publish evidence roots must differ\n' >&2
  exit 1
}
mkdir -p "$EVIDENCE_DIR" "$PRIVATE_ROOT" "$PRIVATE_LOG_DIR" "$DATASET_VIEW/BTCUSDT"
chmod 700 "$RUN_ROOT" "$EVIDENCE_DIR" "$PRIVATE_ROOT" "$PRIVATE_LOG_DIR" "$DATASET_VIEW" "$DATASET_VIEW/BTCUSDT"

publish_failure_evidence() {
  python scripts/write_physical_historical_failure_evidence.py \
    --output-dir "$PUBLISH_FAILURE_DIR" \
    --candidate-sha "$EXPECTED_HEAD" \
    --full-rc-sha "$EXPECTED_BASE" \
    --stage "$CURRENT_STAGE" \
    --exit-code "$1" \
    --run-id "${GITHUB_RUN_ID:-}" \
    --run-attempt "${GITHUB_RUN_ATTEMPT:-}"
}

cleanup() {
  local exit_status=$?
  trap - EXIT
  rm -f "$CORPUS_FILE" 2>/dev/null || true
  if [ "$exit_status" -eq 0 ]; then
    CURRENT_STAGE="success_sentinel_verification"
    if ! python scripts/verify_physical_historical_summary.py \
      --summary "$EVIDENCE_DIR/summary.json" \
      --expected-head "$EXPECTED_HEAD" \
      --expected-base "$EXPECTED_BASE"; then
      printf '[ERROR] successful shell exit without a valid historical PASS sentinel\n' >&2
      exit_status=97
    fi
  fi
  if [ "$exit_status" -ne 0 ]; then
    if ! publish_failure_evidence "$exit_status"; then
      printf '[ERROR] sanitized historical failure evidence publication failed\n' >&2
      exit_status=96
    fi
  fi
  exit "$exit_status"
}
trap cleanup EXIT

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
info() { printf '[INFO] %s\n' "$*"; }

for cmd in git python bash uname shasum; do
  command -v "$cmd" >/dev/null 2>&1 || die "required command missing: $cmd"
done

CURRENT_STAGE="candidate_geometry"
[[ "$EXPECTED_HEAD" =~ ^[0-9a-f]{40}$ ]] || die "EXPECTED_HEAD must be a 40-hex SHA"
[[ "$EXPECTED_BASE" =~ ^[0-9a-f]{40}$ ]] || die "EXPECTED_BASE must be a 40-hex SHA"
[ "$(git rev-parse HEAD)" = "$EXPECTED_HEAD" ] || die "checkout is not exact candidate"
[ -z "$(git status --porcelain)" ] || die "checkout is dirty before physical acceptance"
git cat-file -e "$EXPECTED_BASE^{commit}" 2>/dev/null || die "base SHA is unavailable in checkout"
[ "$(git merge-base "$EXPECTED_BASE" "$EXPECTED_HEAD")" = "$EXPECTED_BASE" ] || die "full-RC is not exact candidate merge-base"
git merge-base --is-ancestor "$BASELINE_SHA" "$EXPECTED_HEAD" || die "candidate is not descendant of physical-acceptance baseline"

CURRENT_STAGE="host_preflight"
[ "$(uname -s)" = "Darwin" ] || die "physical historical acceptance requires Darwin"
[ "$(uname -m)" = "arm64" ] || die "physical historical acceptance requires Apple Silicon arm64"

CURRENT_STAGE="dataset_discovery"
CORPUS_FILE="$CORPUS_FILE" python - <<'PY'
from __future__ import annotations
import glob
import os
from pathlib import Path

home = Path.home().resolve()
patterns = (
    home / "Library/CloudStorage/GoogleDrive-*/My Drive/binance_usdtm_futures_history",
    home / "Library/CloudStorage/GoogleDrive-*/Мой диск/binance_usdtm_futures_history",
    home / "Google Drive/My Drive/binance_usdtm_futures_history",
    home / "Google Диск/Мой диск/binance_usdtm_futures_history",
)
allowed_roots = tuple(
    path.resolve()
    for path in (home / "Library/CloudStorage", home / "Google Drive", home / "Google Диск")
    if path.exists()
)
candidates: set[Path] = set()
for pattern in patterns:
    for value in glob.glob(str(pattern)):
        candidate = Path(value)
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        resolved = candidate.resolve(strict=True)
        if not any(root == resolved or root in resolved.parents for root in allowed_roots):
            raise SystemExit("discovered corpus root escaped approved sync locations")
        candidates.add(resolved)
if len(candidates) != 1:
    raise SystemExit(f"expected exactly one approved synced corpus root, found {len(candidates)}")
root = next(iter(candidates))
summary = root / "download_summary.csv"
raw = root / "BTCUSDT" / "BTCUSDT_5m.parquet"
if summary.is_symlink() or not summary.is_file():
    raise SystemExit("authoritative download_summary.csv is missing or symlinked")
if raw.is_symlink() or not raw.is_file():
    raise SystemExit("authoritative BTCUSDT_5m.parquet is missing or symlinked")
out = Path(os.environ["CORPUS_FILE"])
out.write_text(str(root), encoding="utf-8")
out.chmod(0o600)
print("approved synced corpus root discovered")
PY

CORPUS_ROOT="$(cat "$CORPUS_FILE")"
[ -d "$CORPUS_ROOT" ] || die "private corpus root disappeared"
ln -s "$CORPUS_ROOT/download_summary.csv" "$DATASET_VIEW/download_summary.csv"
ln -s "$CORPUS_ROOT/$CANONICAL_REL" "$DATASET_VIEW/$CANONICAL_REL"

CURRENT_STAGE="dataset_inventory"
CORPUS_ROOT="$CORPUS_ROOT" EVIDENCE_DIR="$EVIDENCE_DIR" python - <<'PY'
from __future__ import annotations
import csv
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["CORPUS_ROOT"])
evidence = Path(os.environ["EVIDENCE_DIR"])
summary = root / "download_summary.csv"
raw = root / "BTCUSDT" / "BTCUSDT_5m.parquet"
with summary.open("r", encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))
if len(rows) != 50:
    raise SystemExit(f"expected 50 download summary rows, got {len(rows)}")
if any(row.get("status", "").strip().lower() != "ok" for row in rows):
    raise SystemExit("download summary contains non-ok rows")
btc = [row for row in rows if row.get("symbol") == "BTCUSDT" and row.get("timeframe") == "5m"]
if len(btc) != 1:
    raise SystemExit(f"expected exactly one BTCUSDT/5m summary row, got {len(btc)}")
row = btc[0]
count = int(row["rows"])
gaps = int(row["gap_events"])
missing = int(row["estimated_missing_5m_rows"])
if count <= 0 or gaps != 0 or missing != 0:
    raise SystemExit("BTCUSDT/5m summary invariants failed")

def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

payload = {
    "schema_version": 1,
    "status": "PASS",
    "summary_rows": len(rows),
    "download_summary_sha256": digest(summary),
    "symbol": row["symbol"],
    "timeframe": row["timeframe"],
    "rows": count,
    "first_open_time": row["first_open_time"],
    "last_open_time": row["last_open_time"],
    "gap_events": gaps,
    "estimated_missing_5m_rows": missing,
    "raw_file_relative": "BTCUSDT/BTCUSDT_5m.parquet",
    "raw_file_size": raw.stat().st_size,
    "raw_file_sha256": digest(raw),
    "raw_market_data_uploaded": False,
}
(evidence / "dataset-inventory.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print(f"dataset inventory PASS: rows={count} gaps={gaps} missing={missing}")
PY

redact_log() {
  local source=$1
  LOG_SOURCE="$source" CORPUS_ROOT="$CORPUS_ROOT" DATASET_VIEW="$DATASET_VIEW" python - <<'PY'
import os
from pathlib import Path
source = Path(os.environ["LOG_SOURCE"])
text = source.read_text(encoding="utf-8", errors="replace")
replacements = (
    (os.environ.get("CORPUS_ROOT", ""), "<DATASET>"),
    (os.environ.get("DATASET_VIEW", ""), "<DATASET_VIEW>"),
    (os.environ.get("GITHUB_WORKSPACE", ""), "<WORKSPACE>"),
    (os.environ.get("RUNNER_TEMP", ""), "<RUNNER_TEMP>"),
    (str(Path.home()), "<HOME>"),
)
for old, new in replacements:
    if old:
        text = text.replace(old, new)
print(text, end="" if text.endswith("\n") else "\n")
PY
}

run_canonical() {
  local mode=$1
  local log="$PRIVATE_LOG_DIR/$mode.log"
  local status
  CURRENT_STAGE="canonical_$mode"
  info "canonical local suite: $mode"
  set +e
  bash scripts/run_local_test_suite.sh "$DATASET_VIEW" "$mode" >"$log" 2>&1
  status=$?
  set -e
  redact_log "$log"
  [ "$status" -eq 0 ] || die "canonical local suite failed in mode=$mode"
}

run_canonical prepare
[ -f .local-tests/pretest/pretest-report.json ] || die "canonical pretest report missing"

CURRENT_STAGE="pretest_redaction"
PRETEST_SOURCE="$GITHUB_WORKSPACE/.local-tests/pretest/pretest-report.json" \
PRETEST_DEST="$EVIDENCE_DIR/pretest-report.json" \
CORPUS_ROOT="$CORPUS_ROOT" DATASET_VIEW="$DATASET_VIEW" python - <<'PY'
import json
import os
from pathlib import Path
source = Path(os.environ["PRETEST_SOURCE"])
destination = Path(os.environ["PRETEST_DEST"])
replacements = (
    (os.environ.get("CORPUS_ROOT", ""), "<DATASET>"),
    (os.environ.get("DATASET_VIEW", ""), "<DATASET_VIEW>"),
    (os.environ.get("GITHUB_WORKSPACE", ""), "<WORKSPACE>"),
    (os.environ.get("RUNNER_TEMP", ""), "<RUNNER_TEMP>"),
    (str(Path.home()), "<HOME>"),
)
def redact(value):
    if isinstance(value, str):
        for old, new in replacements:
            if old:
                value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value
payload = redact(json.loads(source.read_text(encoding="utf-8")))
destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

run_canonical smoke
run_canonical full

LOCAL_TEST_PYTHON="$GITHUB_WORKSPACE/.venv-local-test/bin/python"
[ -x "$LOCAL_TEST_PYTHON" ] || die "canonical local-test Python missing after full suite"

CURRENT_STAGE="result_hashing"
"$LOCAL_TEST_PYTHON" scripts/write_physical_event_storage_evidence.py \
  --result-dir "$GITHUB_WORKSPACE/.local-tests/pretest/results/full-btc" \
  --pretest-report "$GITHUB_WORKSPACE/.local-tests/pretest/pretest-report.json" \
  --disk-root "$GITHUB_WORKSPACE" \
  --output "$EVIDENCE_DIR/event-storage.json"

RESULTS_DIR="$GITHUB_WORKSPACE/.local-tests/pretest/results" \
HASH_OUT="$EVIDENCE_DIR/backtest-result-sha256.txt" python - <<'PY'
import hashlib
import os
from pathlib import Path
root = Path(os.environ["RESULTS_DIR"])
out = Path(os.environ["HASH_OUT"])
if not root.is_dir():
    raise SystemExit("canonical results directory missing")
lines = []
for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    lines.append(f"{digest.hexdigest()}  <RESULTS>/{path.relative_to(root).as_posix()}")
out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
print(f"hashed canonical result files={len(lines)}")
PY

CURRENT_STAGE="summary_write"
EXPECTED_HEAD="$EXPECTED_HEAD" EXPECTED_BASE="$EXPECTED_BASE" EVIDENCE_DIR="$EVIDENCE_DIR" python - <<'PY'
import json
import os
from pathlib import Path
payload = {
    "schema_version": 1,
    "status": "PASS",
    "candidate_sha": os.environ["EXPECTED_HEAD"],
    "full_rc_sha": os.environ["EXPECTED_BASE"],
    "host_os": "Darwin",
    "host_arch": "arm64",
    "canonical_prepare": True,
    "canonical_smoke_compare_restart_replay": True,
    "canonical_full_historical": True,
    "paper_only": True,
    "live_trading_authorized": False,
    "final_24h_soak_complete": False,
    "raw_market_data_uploaded": False,
}
Path(os.environ["EVIDENCE_DIR"], "summary.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

CURRENT_STAGE="evidence_privacy"
CORPUS_ROOT="$CORPUS_ROOT" DATASET_VIEW="$DATASET_VIEW" EVIDENCE_DIR="$EVIDENCE_DIR" python - <<'PY'
import os
from pathlib import Path
root = Path(os.environ["EVIDENCE_DIR"])
expected = {
    "backtest-result-sha256.txt",
    "dataset-inventory.json",
    "event-storage.json",
    "pretest-report.json",
    "summary.json",
}
actual = {path.name for path in root.iterdir() if path.is_file() and not path.is_symlink()}
if actual != expected:
    raise SystemExit(f"historical evidence inventory mismatch: {sorted(actual)}")
if any(path.is_symlink() for path in root.rglob("*")):
    raise SystemExit("historical evidence contains a symlink")
needles = tuple(
    value
    for value in (
        os.environ.get("CORPUS_ROOT", ""),
        os.environ.get("DATASET_VIEW", ""),
        os.environ.get("GITHUB_WORKSPACE", ""),
        os.environ.get("RUNNER_TEMP", ""),
        str(Path.home()),
        "/Users/",
    )
    if value
)
for path in root.iterdir():
    if path.suffix.lower() in {".parquet", ".csv", ".db", ".sqlite", ".sqlite3"}:
        raise SystemExit("raw/database data entered historical evidence")
    text = path.read_text(encoding="utf-8", errors="ignore")
    if any(needle in text for needle in needles):
        raise SystemExit(f"private absolute path leaked into evidence: {path.name}")
print("historical evidence privacy/closed-inventory audit PASS")
PY

CURRENT_STAGE="summary_verification"
python scripts/verify_physical_historical_summary.py \
  --summary "$EVIDENCE_DIR/summary.json" \
  --expected-head "$EXPECTED_HEAD" \
  --expected-base "$EXPECTED_BASE"

CURRENT_STAGE="evidence_publish"
[ ! -e "$PUBLISH_EVIDENCE_DIR" ] || die "publishable historical evidence path already exists"
mkdir -p "$PUBLISH_EVIDENCE_DIR"
chmod 700 "$PUBLISH_EVIDENCE_DIR"
for name in backtest-result-sha256.txt dataset-inventory.json event-storage.json pretest-report.json summary.json; do
  cp "$EVIDENCE_DIR/$name" "$PUBLISH_EVIDENCE_DIR/$name"
done

CURRENT_STAGE="success_sentinel_verification"
info "physical historical acceptance PASS"