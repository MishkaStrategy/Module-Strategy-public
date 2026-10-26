from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from modular_system_acceptance.contracts import AcceptanceOwnerError
from modular_system_acceptance.owner import PhaseExecutionContext
from modular_system_acceptance.production_backtest_proof import (
    VerifiedBacktestDeterminismBinding,
    VerifiedBacktestRunBinding,
)
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity

IDENTITY = TrustedAcceptanceIdentity(
    full_rc_sha="a" * 40,
    dataset_id="btc-usdtm-5m",
    dataset_digest="b" * 64,
    strategy_id="strategy-btc-v1",
    strategy_digest="c" * 64,
    config_digest="d" * 64,
)


@dataclass(frozen=True)
class Outcome:
    output_directory: Path
    run_id: str
    restart_verified: bool
    application_run_id: str | None


def write_result(
    root: Path,
    *,
    run_id: str = "backtest-run",
    application_run_id: str = "backtest-run",
    restart_verified: bool = False,
    continuous: object | None = None,
    restarted: object | None = None,
    marker: str = "same",
) -> Outcome:
    root.mkdir(parents=True)
    payload = {
        "application_run_id": application_run_id,
        "core_run_id": run_id,
        "restart_verified": restart_verified,
        "continuous_sha256": continuous,
        "restarted_sha256": restarted,
        "marker": marker,
    }
    (root / "application-run.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    return Outcome(
        output_directory=root,
        run_id=run_id,
        restart_verified=restart_verified,
        application_run_id=application_run_id,
    )


def verify_manifest(path: Path) -> dict[str, object]:
    raw = (path / "application-run.json").read_bytes()
    payload = json.loads(raw)
    run_id = payload["core_run_id"]
    application_run_id = payload["application_run_id"]
    manifest: dict[str, object] = {
        "schema_version": "1",
        "run_id": run_id,
        "files": {"application-run.json": hashlib.sha256(raw).hexdigest()},
    }
    if application_run_id != run_id:
        manifest["application_run_id"] = application_run_id
    return manifest


def context() -> PhaseExecutionContext:
    return cast(PhaseExecutionContext, object())


def test_verified_run_binding_builds_proof_from_manifest(tmp_path: Path) -> None:
    outcome = write_result(tmp_path / "run")
    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=lambda _: outcome,
        verify_manifest=verify_manifest,
    )

    proof = binding(context())

    assert proof.run_id == "backtest-run"
    assert len(proof.artifact_digest) == 64
    assert proof.full_rc_sha == IDENTITY.full_rc_sha
    assert proof.dataset_digest == IDENTITY.dataset_digest
    assert proof.strategy_digest == IDENTITY.strategy_digest
    assert proof.config_digest == IDENTITY.config_digest
    assert proof.restart_verified is False
    assert proof.continuous_sha256 is None
    assert proof.restarted_sha256 is None


def test_verified_recovery_binding_extracts_restart_equivalence(tmp_path: Path) -> None:
    restart_digest = "e" * 64
    outcome = write_result(
        tmp_path / "recovery",
        restart_verified=True,
        continuous=restart_digest,
        restarted=restart_digest,
    )
    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=lambda _: outcome,
        verify_manifest=verify_manifest,
        require_restart=True,
    )

    proof = binding(context())

    assert proof.restart_verified is True
    assert proof.continuous_sha256 == restart_digest
    assert proof.restarted_sha256 == restart_digest


def test_recovery_binding_fails_closed_on_restart_mismatch(tmp_path: Path) -> None:
    outcome = write_result(
        tmp_path / "recovery",
        restart_verified=True,
        continuous="e" * 64,
        restarted="f" * 64,
    )
    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=lambda _: outcome,
        verify_manifest=verify_manifest,
        require_restart=True,
    )

    with pytest.raises(AcceptanceOwnerError, match="restart equivalence"):
        binding(context())


def test_manifest_run_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    outcome = write_result(tmp_path / "run")

    def wrong_manifest(path: Path) -> dict[str, object]:
        manifest = verify_manifest(path)
        manifest["run_id"] = "different-run"
        return manifest

    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=lambda _: outcome,
        verify_manifest=wrong_manifest,
    )

    with pytest.raises(AcceptanceOwnerError, match="manifest run_id differs"):
        binding(context())


def test_manifest_application_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    outcome = write_result(
        tmp_path / "run",
        run_id="core-run",
        application_run_id="application-run",
    )

    def wrong_manifest(path: Path) -> dict[str, object]:
        manifest = verify_manifest(path)
        manifest["application_run_id"] = "different-application"
        return manifest

    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=lambda _: outcome,
        verify_manifest=wrong_manifest,
    )

    with pytest.raises(AcceptanceOwnerError, match="manifest application_run_id differs"):
        binding(context())


def test_application_restart_digest_type_fails_as_owner_error(tmp_path: Path) -> None:
    outcome = write_result(
        tmp_path / "run",
        restart_verified=True,
        continuous=123,
        restarted="e" * 64,
    )
    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=lambda _: outcome,
        verify_manifest=verify_manifest,
    )

    with pytest.raises(AcceptanceOwnerError, match="continuous_sha256 is invalid"):
        binding(context())


def test_run_failure_is_sanitized(tmp_path: Path) -> None:
    del tmp_path

    def fail(_: PhaseExecutionContext) -> Outcome:
        raise RuntimeError("private path should not escape")

    binding = VerifiedBacktestRunBinding(
        identity=IDENTITY,
        run=fail,
        verify_manifest=verify_manifest,
    )

    with pytest.raises(AcceptanceOwnerError, match="authoritative Backtest run failed"):
        binding(context())


def test_determinism_binding_reverifies_distinct_durable_roots(tmp_path: Path) -> None:
    left = write_result(tmp_path / "left")
    right = write_result(tmp_path / "right")
    binding = VerifiedBacktestDeterminismBinding(
        left=lambda: left.output_directory,
        right=lambda: right.output_directory,
        verify_manifest=verify_manifest,
    )

    proof = binding(context())

    assert proof.left_artifact_digest == proof.right_artifact_digest


def test_determinism_binding_surfaces_content_difference(tmp_path: Path) -> None:
    left = write_result(tmp_path / "left", marker="left")
    right = write_result(tmp_path / "right", marker="right")
    binding = VerifiedBacktestDeterminismBinding(
        left=lambda: left.output_directory,
        right=lambda: right.output_directory,
        verify_manifest=verify_manifest,
    )

    proof = binding(context())

    assert proof.left_artifact_digest != proof.right_artifact_digest


def test_determinism_rejects_same_result_root(tmp_path: Path) -> None:
    outcome = write_result(tmp_path / "same")
    binding = VerifiedBacktestDeterminismBinding(
        left=lambda: outcome.output_directory,
        right=lambda: outcome.output_directory,
        verify_manifest=verify_manifest,
    )

    with pytest.raises(AcceptanceOwnerError, match="two distinct durable"):
        binding(context())
