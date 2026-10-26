#!/usr/bin/env bash
set -euo pipefail

role="${1:-}"
case "$role" in
  fast|docker|macos|backtester) ;;
  *)
    echo "runner preflight: unsupported role '$role'; expected fast, docker, macos, or backtester" >&2
    exit 2
    ;;
esac

required_commands=(bash git df awk sed mktemp tar find rm)
for command_name in "${required_commands[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "runner preflight: missing required command: $command_name" >&2
    exit 1
  fi
done

if command -v sha256sum >/dev/null 2>&1; then
  checksum_command="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  checksum_command="shasum -a 256"
else
  echo "runner preflight: neither sha256sum nor shasum is available" >&2
  exit 1
fi

workspace="${GITHUB_WORKSPACE:-$PWD}"
temp_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"

for directory in "$workspace" "$temp_root"; do
  if [[ ! -d "$directory" ]]; then
    echo "runner preflight: required directory does not exist: $directory" >&2
    exit 1
  fi
  probe="$(mktemp "$directory/.module-strategy-preflight.XXXXXX")"
  printf 'ok\n' >"$probe"
  rm -f "$probe"
done

reclaim_backtester_scratch() {
  local before_kib after_kib candidate
  before_kib="$(df -Pk "$workspace" | awk 'NR == 2 {print $4}')"

  while IFS= read -r -d '' candidate; do
    rm -rf -- "$candidate"
  done < <(
    find "$temp_root" -mindepth 1 -maxdepth 1 -type d \
      \( \
        -name 'module-strategy-*' -o \
        -name 'setup-uv-cache-*' -o \
        -name 'pinned-uv-*' -o \
        -name 'auditor-dist-*' -o \
        -name 'auditor-clean-*' -o \
        -name 'strategy-factory-*' -o \
        -name 'canonical-strategy-*' \
      \) -print0
  )

  # HOME-level package caches may be in use by another job on this runner.
  # Reclaim only this job's RUNNER_TEMP scratch; never recursively delete shared caches.

  after_kib="$(df -Pk "$workspace" | awk 'NR == 2 {print $4}')"
  if [[ "$before_kib" =~ ^[0-9]+$ && "$after_kib" =~ ^[0-9]+$ ]]; then
    printf 'runner preflight cleanup: role=backtester reclaimed_mib=%s free_before_gib=%s free_after_gib=%s\n' \
      "$(((after_kib - before_kib) / 1024))" \
      "$((before_kib / 1024 / 1024))" \
      "$((after_kib / 1024 / 1024))"
  fi
}

if [[ "$role" == "backtester" ]]; then
  reclaim_backtester_scratch
fi

case "$role" in
  fast)
    # Ordinary lint/test/package jobs have a small bounded footprint. One GiB
    # remains the fail-closed disk floor; below two GiB is an early disk signal.
    # RAM is observed for diagnostics but is not a scheduling/preflight gate.
    high_water_disk_gib=2
    critical_disk_gib=1
    ;;
  docker)
    high_water_disk_gib=15
    critical_disk_gib=10
    ;;
  macos)
    high_water_disk_gib=12
    critical_disk_gib=8
    ;;
  backtester)
    high_water_disk_gib=30
    critical_disk_gib=20
    min_ram_gib=8
    ;;
esac

free_kib="$(df -Pk "$workspace" | awk 'NR == 2 {print $4}')"
if [[ ! "$free_kib" =~ ^[0-9]+$ ]]; then
  echo "runner preflight: could not determine free disk space for $workspace" >&2
  exit 1
fi
critical_disk_kib=$((critical_disk_gib * 1024 * 1024))
high_water_disk_kib=$((high_water_disk_gib * 1024 * 1024))

if (( free_kib < critical_disk_kib )); then
  echo "runner preflight: critical disk pressure for role $role: $((free_kib / 1024 / 1024)) GiB free, ${critical_disk_gib} GiB required" >&2
  exit 1
fi

if (( free_kib < high_water_disk_kib )); then
  disk_pressure_state="high-water"
  echo "runner preflight warning: high-water disk pressure for role $role: $((free_kib / 1024)) MiB free; warning below ${high_water_disk_gib} GiB, critical below ${critical_disk_gib} GiB" >&2
else
  disk_pressure_state="healthy"
fi

printf 'runner preflight capacity: role=%s state=%s disk_free_mib=%s disk_free_gib=%s high_water_gib=%s critical_gib=%s\n' \
  "$role" \
  "$disk_pressure_state" \
  "$((free_kib / 1024))" \
  "$((free_kib / 1024 / 1024))" \
  "$high_water_disk_gib" \
  "$critical_disk_gib"

kernel="$(uname -s)"
case "$kernel" in
  Linux)
    ram_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    ;;
  Darwin)
    if ! command -v sysctl >/dev/null 2>&1; then
      echo "runner preflight: sysctl is required on macOS" >&2
      exit 1
    fi
    ram_bytes="$(sysctl -n hw.memsize)"
    ram_kib=$((ram_bytes / 1024))
    ;;
  *)
    echo "runner preflight: unsupported operating system: $kernel" >&2
    exit 1
    ;;
esac

if [[ ! "$ram_kib" =~ ^[0-9]+$ ]]; then
  echo "runner preflight: could not determine installed RAM" >&2
  exit 1
fi

# RAM capacity is a hard gate only for the quantitative backtester workload.
# All other roles may run on low-memory hosts and rely on the OS/swap policy.
if [[ "$role" == "backtester" ]]; then
  min_ram_kib=$((min_ram_gib * 1024 * 1024))
  if (( ram_kib < min_ram_kib )); then
    echo "runner preflight: insufficient RAM for role $role: $((ram_kib / 1024 / 1024)) GiB installed, ${min_ram_gib} GiB required" >&2
    exit 1
  fi
fi

if [[ "$role" == "docker" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "runner preflight: Docker CLI is required for docker role" >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "runner preflight: Docker daemon is unavailable or runner permissions are insufficient" >&2
    exit 1
  fi
fi

if [[ "$role" == "macos" && "$kernel" != "Darwin" ]]; then
  echo "runner preflight: macos role requires Darwin, found $kernel" >&2
  exit 1
fi

printf 'runner preflight passed: role=%s os=%s git=%s disk_pressure=%s disk_free_gib=%s high_water_gib=%s critical_gib=%s ram_gib=%s checksum=%s workspace=%s temp=%s\n' \
  "$role" \
  "$kernel" \
  "$(git --version | sed 's/^git version //')" \
  "$disk_pressure_state" \
  "$((free_kib / 1024 / 1024))" \
  "$high_water_disk_gib" \
  "$critical_disk_gib" \
  "$((ram_kib / 1024 / 1024))" \
  "$checksum_command" \
  "$workspace" \
  "$temp_root"
