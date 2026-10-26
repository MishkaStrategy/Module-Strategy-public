#!/usr/bin/env bash
set -euo pipefail

version="${ACTIONLINT_VERSION:-1.7.12}"
destination="${1:-${RUNNER_TEMP:-/tmp}/actionlint-bin}"

case "$version" in
  1.7.12) ;;
  *)
    echo "actionlint installer: unsupported version '$version'; update the reviewed pin first" >&2
    exit 2
    ;;
esac

for command_name in curl tar mktemp grep awk uname install head; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "actionlint installer: missing required command: $command_name" >&2
    exit 1
  fi
done

case "$(uname -s)" in
  Linux) os="linux" ;;
  Darwin) os="darwin" ;;
  *)
    echo "actionlint installer: unsupported operating system: $(uname -s)" >&2
    exit 1
    ;;
esac

case "$(uname -m)" in
  x86_64|amd64) architecture="amd64" ;;
  arm64|aarch64) architecture="arm64" ;;
  *)
    echo "actionlint installer: unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

archive="actionlint_${version}_${os}_${architecture}.tar.gz"
base_url="https://github.com/rhysd/actionlint/releases/download/v${version}"
work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/actionlint-install.XXXXXX")"
trap 'rm -rf "$work"' EXIT

curl --fail --location --proto '=https' --tlsv1.2 \
  --retry 3 --retry-all-errors \
  --output "$work/$archive" \
  "$base_url/$archive"
curl --fail --location --proto '=https' --tlsv1.2 \
  --retry 3 --retry-all-errors \
  --output "$work/checksums.txt" \
  "$base_url/actionlint_${version}_checksums.txt"

expected="$(awk -v name="$archive" '$2 == name {print $1}' "$work/checksums.txt")"
if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
  echo "actionlint installer: official checksum is missing or malformed for $archive" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$work/$archive" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$work/$archive" | awk '{print $1}')"
else
  echo "actionlint installer: no SHA-256 utility is available" >&2
  exit 1
fi

if [[ "$actual" != "$expected" ]]; then
  echo "actionlint installer: checksum mismatch for $archive" >&2
  exit 1
fi

mkdir -p "$destination"
tar -xzf "$work/$archive" -C "$work" actionlint
install -m 0755 "$work/actionlint" "$destination/actionlint"

reported="$($destination/actionlint -version | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
if [[ "$reported" != "$version" ]]; then
  echo "actionlint installer: expected version $version, found $reported" >&2
  exit 1
fi

printf 'actionlint %s installed at %s (sha256=%s)\n' \
  "$reported" "$destination/actionlint" "$actual"
