#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <major.minor>" >&2
  exit 64
fi

requested="$1"
if [[ ! "$requested" =~ ^3\.(12|13)$ ]]; then
  echo "unsupported CI Python version: $requested" >&2
  exit 64
fi

validate_python() {
  local candidate="$1"
  [[ -x "$candidate" ]] || return 1
  "$candidate" - "$requested" "${RUNNER_ARCH:-}" <<'PY' >/dev/null 2>&1
from __future__ import annotations

import platform
import sys

requested = tuple(int(part) for part in sys.argv[1].split("."))
expected_runner_arch = sys.argv[2].strip().upper()
if sys.version_info[:2] != requested:
    raise SystemExit(1)
if expected_runner_arch == "ARM64" and platform.machine().lower() not in {"arm64", "aarch64"}:
    raise SystemExit(1)
PY
}

candidates=(
  "$(command -v "python${requested}" 2>/dev/null || true)"
  "/opt/homebrew/bin/python${requested}"
  "/usr/local/bin/python${requested}"
  "/Library/Frameworks/Python.framework/Versions/${requested}/bin/python${requested}"
  "$HOME/.local/bin/python${requested}"
)

python_path=""
for candidate in "${candidates[@]}"; do
  if [[ -n "$candidate" ]] && validate_python "$candidate"; then
    python_path="$candidate"
    break
  fi
done

if [[ -z "$python_path" ]]; then
  temp_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
  bootstrap_dir="$temp_root/setup-adapter-uv-bootstrap-${requested//./-}"
  uv_bin="$(command -v uv 2>/dev/null || true)"

  if [[ -z "$uv_bin" ]]; then
    rm -rf "$bootstrap_dir"
    python3 -m venv "$bootstrap_dir"
    "$bootstrap_dir/bin/python" -m pip install --upgrade pip uv
    uv_bin="$bootstrap_dir/bin/uv"
  fi

  export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
  "$uv_bin" python install "$requested"
  python_path="$($uv_bin python find "$requested")"

  if ! validate_python "$python_path"; then
    echo "resolved interpreter failed version/architecture validation: $python_path" >&2
    exit 1
  fi
fi

"$python_path" - <<'PY'
from __future__ import annotations

import platform
import sys

print(f"resolved_python={sys.executable}", file=sys.stderr)
print(f"resolved_version={platform.python_version()}", file=sys.stderr)
print(f"resolved_architecture={platform.machine()}", file=sys.stderr)
PY

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'python=%s\n' "$python_path" >> "$GITHUB_OUTPUT"
else
  printf '%s\n' "$python_path"
fi
