from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runner_preflight.sh"
POLICY = ROOT / ".github" / "RUNNER_LABELS.md"


def _run_preflight(
    tmp_path: Path,
    *,
    role: str,
    free_kib: int,
    ram_kib: int | None = None,
    kernel: str | None = None,
    home_dir: Path | None = None,
    runner_temp_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_df = fake_bin / "df"
    fake_df.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        "printf 'fakefs 100000000 0 %s 0%% /\\n' \"${FAKE_FREE_KIB:?}\"\n",
        encoding="utf-8",
    )
    fake_df.chmod(0o755)

    if ram_kib is not None:
        real_awk = shutil.which("awk")
        assert real_awk is not None
        fake_awk = fake_bin / "awk"
        fake_awk.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'if [[ "${*: -1}" == "/proc/meminfo" ]]; then\n'
            "  printf '%s\\n' \"${FAKE_RAM_KIB:?}\"\n"
            "  exit 0\n"
            "fi\n"
            f'exec "{real_awk}" "$@"\n',
            encoding="utf-8",
        )
        fake_awk.chmod(0o755)

        fake_sysctl = fake_bin / "sysctl"
        fake_sysctl.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'if [[ "$#" -eq 2 && "$1" == "-n" && "$2" == "hw.memsize" ]]; then\n'
            '  ram_kib="${FAKE_RAM_KIB:?}"\n'
            "  printf '%s\\n' \"$((ram_kib * 1024))\"\n"
            "  exit 0\n"
            "fi\n"
            "printf 'unexpected fake sysctl invocation\\n' >&2\n"
            "exit 64\n",
            encoding="utf-8",
        )
        fake_sysctl.chmod(0o755)

    if kernel is not None:
        if kernel not in {"Linux", "Darwin"}:
            raise ValueError("test kernel must be Linux or Darwin")
        real_uname = shutil.which("uname")
        assert real_uname is not None
        fake_uname = fake_bin / "uname"
        fake_uname.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'if [[ "$#" -eq 1 && "$1" == "-s" ]]; then\n'
            "  printf '%s\\n' \"${FAKE_KERNEL:?}\"\n"
            "  exit 0\n"
            "fi\n"
            f'exec "{real_uname}" "$@"\n',
            encoding="utf-8",
        )
        fake_uname.chmod(0o755)

    workspace = tmp_path / "workspace"
    runner_temp = runner_temp_dir or tmp_path / "runner-temp"
    home = home_dir or tmp_path / "home"
    workspace.mkdir()
    runner_temp.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_FREE_KIB": str(free_kib),
            "GITHUB_WORKSPACE": str(workspace),
            "RUNNER_TEMP": str(runner_temp),
            "HOME": str(home),
        }
    )
    if ram_kib is not None:
        env["FAKE_RAM_KIB"] = str(ram_kib)
    if kernel is not None:
        env["FAKE_KERNEL"] = kernel

    return subprocess.run(
        ["bash", str(SCRIPT), role],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _run_fast_preflight(tmp_path: Path, *, free_kib: int) -> subprocess.CompletedProcess[str]:
    return _run_preflight(tmp_path, role="fast", free_kib=free_kib)


def test_fast_disk_pressure_healthy(tmp_path: Path) -> None:
    result = _run_fast_preflight(tmp_path, free_kib=3 * 1024 * 1024)

    assert result.returncode == 0, result.stderr
    assert "state=healthy" in result.stdout
    assert "disk_pressure=healthy" in result.stdout
    assert "high_water_gib=2" in result.stdout
    assert "critical_gib=1" in result.stdout
    assert "high-water disk pressure" not in result.stderr


def test_fast_disk_pressure_high_water_warns_but_continues(tmp_path: Path) -> None:
    result = _run_fast_preflight(tmp_path, free_kib=1536 * 1024)

    assert result.returncode == 0, result.stderr
    assert "state=high-water" in result.stdout
    assert "disk_pressure=high-water" in result.stdout
    assert "high-water disk pressure for role fast" in result.stderr
    assert "warning below 2 GiB, critical below 1 GiB" in result.stderr


def test_fast_disk_pressure_critical_fails_closed(tmp_path: Path) -> None:
    result = _run_fast_preflight(tmp_path, free_kib=512 * 1024)

    assert result.returncode == 1
    assert "critical disk pressure for role fast" in result.stderr
    assert "1 GiB required" in result.stderr
    assert "runner preflight passed" not in result.stdout


@pytest.mark.parametrize("kernel", ("Linux", "Darwin"))
def test_fast_low_ram_is_not_a_preflight_gate(tmp_path: Path, kernel: str) -> None:
    result = _run_preflight(
        tmp_path,
        role="fast",
        free_kib=3 * 1024 * 1024,
        ram_kib=512 * 1024,
        kernel=kernel,
    )

    assert result.returncode == 0, result.stderr
    assert "runner preflight passed: role=fast" in result.stdout
    assert "insufficient RAM" not in result.stderr


@pytest.mark.parametrize("kernel", ("Linux", "Darwin"))
def test_backtester_retains_eight_gib_ram_floor(tmp_path: Path, kernel: str) -> None:
    result = _run_preflight(
        tmp_path,
        role="backtester",
        free_kib=31 * 1024 * 1024,
        ram_kib=7 * 1024 * 1024,
        kernel=kernel,
    )

    assert result.returncode == 1
    assert "insufficient RAM for role backtester" in result.stderr
    assert "7 GiB installed, 8 GiB required" in result.stderr
    assert "runner preflight passed" not in result.stdout


def test_backtester_preserves_shared_home_caches_and_cleans_job_scratch(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    uv_sentinel = home / ".cache" / "uv" / ".tmpconcurrent" / "pygments" / "lexers" / "active"
    pip_sentinel = home / ".cache" / "pip" / "http-v2" / "active"
    uv_sentinel.parent.mkdir(parents=True)
    pip_sentinel.parent.mkdir(parents=True)
    uv_sentinel.write_text("uv writer still owns this temporary tree", encoding="utf-8")
    pip_sentinel.write_text("another job may be installing packages", encoding="utf-8")

    runner_temp = tmp_path / "runner-temp"
    stale_scratch = runner_temp / "strategy-factory-stale"
    stale_scratch.mkdir(parents=True)
    (stale_scratch / "scratch.bin").write_bytes(b"job-owned scratch")

    result = _run_preflight(
        tmp_path,
        role="backtester",
        free_kib=31 * 1024 * 1024,
        ram_kib=16 * 1024 * 1024,
        kernel="Linux",
        home_dir=home,
        runner_temp_dir=runner_temp,
    )

    assert result.returncode == 0, result.stderr
    assert "runner preflight passed: role=backtester" in result.stdout
    assert uv_sentinel.read_text(encoding="utf-8") == "uv writer still owns this temporary tree"
    assert pip_sentinel.read_text(encoding="utf-8") == "another job may be installing packages"
    assert not stale_scratch.exists()


def test_only_backtester_has_a_ram_minimum() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    policy = POLICY.read_text(encoding="utf-8")

    for role in ("fast", "docker", "macos"):
        block_start = script.index(f"  {role})")
        next_case = script.find("    ;;", block_start)
        block = script[block_start:next_case]
        assert "min_ram_gib=" not in block
        assert f"| `{role}` | no minimum |" in policy

    backtester_start = script.index("  backtester)")
    backtester_end = script.find("    ;;", backtester_start)
    backtester_block = script[backtester_start:backtester_end]
    assert "min_ram_gib=8" in backtester_block
    assert "| `backtester` | >= 8 GiB |" in policy
    assert 'if [[ "$role" == "backtester" ]]; then' in script


def test_all_role_disk_thresholds_are_documented_and_ordered() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    policy = POLICY.read_text(encoding="utf-8")

    expected = {
        "fast": (2, 1),
        "docker": (15, 10),
        "macos": (12, 8),
        "backtester": (30, 20),
    }
    for role, (high_water, critical) in expected.items():
        assert high_water > critical
        block_start = script.index(f"  {role})")
        next_case = script.find("    ;;", block_start)
        block = script[block_start:next_case]
        assert f"high_water_disk_gib={high_water}" in block
        assert f"critical_disk_gib={critical}" in block
        assert f"| `{role}` |" in policy
        assert f">= {high_water} GiB free" in policy
        assert f"< {critical} GiB" in policy


def test_preflight_emits_structured_capacity_contract() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for fragment in (
        "runner preflight capacity: role=%s state=%s",
        "disk_free_mib=%s",
        "high_water_gib=%s",
        "critical_gib=%s",
        "disk_pressure=%s",
    ):
        assert fragment in script
