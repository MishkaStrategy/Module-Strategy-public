from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import modular_research_runner.output_manifest as output_manifest
from modular_research_runner.engine import BacktestRunReference
from modular_research_runner.output_manifest import (
    OutputManifestError,
    output_manifest_path,
    read_output_manifest,
    verify_output_manifest,
    write_output_manifest,
)


def _reference(root: Path) -> BacktestRunReference:
    root.mkdir(parents=True)
    (root / "metrics.json").write_text('{"profit_factor":1.2}\n', encoding="utf-8")
    nested = root / "trades"
    nested.mkdir()
    (nested / "part-000.csv").write_text("id,pnl\n1,3.5\n", encoding="utf-8")
    return BacktestRunReference(
        requested_run_id="run-requested",
        engine_run_id="engine-run",
        output_directory=root,
        bars_processed=500,
        restart_verified=True,
    )


def _single_artifact_reference(root: Path, payload: bytes) -> BacktestRunReference:
    root.mkdir(parents=True)
    (root / "artifact.bin").write_bytes(payload)
    return BacktestRunReference("run", "engine", root, 1, True)


def test_output_manifest_binds_run_identity_and_exact_artifact_set(tmp_path: Path) -> None:
    reference = _reference(tmp_path / "output")
    manifest = write_output_manifest(reference)

    assert manifest == output_manifest_path(reference)
    assert manifest.parent == reference.output_directory.parent
    assert manifest.parent != reference.output_directory
    assert verify_output_manifest(reference)
    decoded = read_output_manifest(reference)
    assert decoded["requested_run_id"] == reference.requested_run_id
    assert decoded["engine_run_id"] == reference.engine_run_id
    assert decoded["output_directory"] == reference.output_directory.resolve()
    assert {item["path"] for item in decoded["artifacts"]} == {
        "metrics.json",
        "trades/part-000.csv",
    }


@pytest.mark.parametrize("mutation", ["modify", "delete", "extra"])
def test_output_manifest_rejects_artifact_set_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    reference = _reference(tmp_path / "output")
    write_output_manifest(reference)

    if mutation == "modify":
        (reference.output_directory / "metrics.json").write_text("tampered\n", encoding="utf-8")
    elif mutation == "delete":
        (reference.output_directory / "metrics.json").unlink()
    else:
        (reference.output_directory / "unexpected.bin").write_bytes(b"unexpected")

    assert not verify_output_manifest(reference)
    with pytest.raises(OutputManifestError):
        read_output_manifest(reference)


def test_output_manifest_rejects_tampered_run_identity(tmp_path: Path) -> None:
    reference = _reference(tmp_path / "output")
    manifest = write_output_manifest(reference)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["requested_run_id"] = "different-run"
    manifest.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

    assert not verify_output_manifest(reference)
    with pytest.raises(OutputManifestError, match="run ID does not match"):
        read_output_manifest(reference)


def test_output_manifest_rejects_symlink_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        (root / "linked.txt").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    reference = BacktestRunReference("run", "engine", root, 1, True)

    with pytest.raises(OutputManifestError, match="symlink"):
        write_output_manifest(reference)


def test_output_manifest_rejects_missing_or_symlink_manifest(tmp_path: Path) -> None:
    reference = _reference(tmp_path / "output")
    assert not verify_output_manifest(reference)

    outside = tmp_path / "outside-manifest.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = output_manifest_path(reference)
    try:
        manifest.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    assert not verify_output_manifest(reference)


def test_output_manifest_enforces_per_artifact_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _single_artifact_reference(tmp_path / "output", b"12345")
    monkeypatch.setattr(output_manifest, "MAX_ARTIFACT_BYTES", 4)

    with pytest.raises(OutputManifestError, match="artifact exceeds its size limit"):
        write_output_manifest(reference)


def test_output_manifest_enforces_total_artifact_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    (root / "first.bin").write_bytes(b"1234")
    (root / "second.bin").write_bytes(b"5678")
    reference = BacktestRunReference("run", "engine", root, 2, True)
    monkeypatch.setattr(output_manifest, "MAX_ARTIFACT_BYTES", 8)
    monkeypatch.setattr(output_manifest, "MAX_TOTAL_ARTIFACT_BYTES", 6)

    with pytest.raises(OutputManifestError, match="total size limit"):
        write_output_manifest(reference)


def test_output_manifest_rechecks_declared_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _single_artifact_reference(tmp_path / "output", b"12345")
    write_output_manifest(reference)
    monkeypatch.setattr(output_manifest, "MAX_ARTIFACT_BYTES", 4)

    with pytest.raises(OutputManifestError, match="manifest artifact exceeds"):
        read_output_manifest(reference)
    assert not verify_output_manifest(reference)


def test_output_manifest_rejects_file_replaced_between_inspection_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _single_artifact_reference(tmp_path / "output", b"original")
    target = reference.output_directory / "artifact.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    original_open = output_manifest.os.open
    swapped = False

    def swapping_open(
        path: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == target:
            swapped = True
            target.unlink()
            try:
                target.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(output_manifest.os, "open", swapping_open)

    with pytest.raises(OutputManifestError):
        write_output_manifest(reference)
