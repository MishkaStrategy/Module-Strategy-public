from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, cast

from .contracts import AcceptanceErrorCode, AcceptanceOwnerError
from .owner import PhaseExecutionContext
from .production_backtest import BacktestDeterminismProof, BacktestRunProof
from .production_identity import TrustedAcceptanceIdentity, require_identifier, require_sha256

_MAX_APPLICATION_RUN_BYTES = 1024 * 1024


class BacktestRunOutcomePort(Protocol):
    @property
    def output_directory(self) -> Path: ...

    @property
    def run_id(self) -> str: ...

    @property
    def restart_verified(self) -> bool: ...

    @property
    def application_run_id(self) -> str | None: ...


BacktestRunOwner = Callable[[PhaseExecutionContext], BacktestRunOutcomePort]
BacktestResultResolver = Callable[[], Path]
BacktestManifestVerifier = Callable[[Path], Mapping[str, object]]


class VerifiedBacktestRunBinding:
    """Build a trusted proof from one authoritative Backtest run and its artifacts."""

    def __init__(
        self,
        *,
        identity: TrustedAcceptanceIdentity,
        run: BacktestRunOwner,
        verify_manifest: BacktestManifestVerifier,
        require_restart: bool = False,
    ) -> None:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(run):
            raise TypeError("run must be callable")
        if not callable(verify_manifest):
            raise TypeError("verify_manifest must be callable")
        if not isinstance(require_restart, bool):
            raise TypeError("require_restart must be bool")
        self._identity = identity
        self._run = run
        self._verify_manifest = verify_manifest
        self._require_restart = require_restart

    def __call__(self, context: PhaseExecutionContext) -> BacktestRunProof:
        try:
            outcome = self._run(context)
        except AcceptanceOwnerError:
            raise
        except Exception as exc:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_FAILURE,
                "authoritative Backtest run failed",
            ) from exc
        return self._proof_from_outcome(outcome)

    def _proof_from_outcome(self, outcome: BacktestRunOutcomePort) -> BacktestRunProof:
        output_directory, run_id, application_run_id, restart_verified = _outcome_identity(outcome)
        first_manifest_digest = _verified_manifest_digest(
            output_directory,
            self._verify_manifest,
            expected_run_id=run_id,
            expected_application_run_id=application_run_id,
        )
        application = _read_application_run(output_directory)
        second_manifest_digest = _verified_manifest_digest(
            output_directory,
            self._verify_manifest,
            expected_run_id=run_id,
            expected_application_run_id=application_run_id,
        )
        if first_manifest_digest != second_manifest_digest:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest result changed while authoritative proof was being verified",
            )

        _require_application_identity(
            application,
            run_id=run_id,
            application_run_id=application_run_id,
        )
        metadata_restart = application.get("restart_verified")
        if not isinstance(metadata_restart, bool) or metadata_restart != restart_verified:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest restart status differs between outcome and verified artifacts",
            )
        continuous = _optional_digest(application.get("continuous_sha256"), "continuous_sha256")
        restarted = _optional_digest(application.get("restarted_sha256"), "restarted_sha256")
        if (continuous is None) != (restarted is None):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest restart artifacts contain an incomplete digest pair",
            )
        if self._require_restart and (
            not restart_verified
            or continuous is None
            or restarted is None
            or continuous != restarted
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_FAILURE,
                "Backtest recovery run did not prove restart equivalence",
            )

        return BacktestRunProof(
            run_id=run_id,
            artifact_digest=second_manifest_digest,
            full_rc_sha=self._identity.full_rc_sha,
            dataset_digest=self._identity.dataset_digest,
            strategy_digest=self._identity.strategy_digest,
            config_digest=self._identity.config_digest,
            restart_verified=restart_verified,
            continuous_sha256=continuous,
            restarted_sha256=restarted,
        )


class VerifiedBacktestDeterminismBinding:
    """Re-verify two durable Backtest result roots without ephemeral proof state."""

    def __init__(
        self,
        *,
        left: BacktestResultResolver,
        right: BacktestResultResolver,
        verify_manifest: BacktestManifestVerifier,
    ) -> None:
        if not callable(left) or not callable(right):
            raise TypeError("left and right result resolvers must be callable")
        if not callable(verify_manifest):
            raise TypeError("verify_manifest must be callable")
        self._left = left
        self._right = right
        self._verify_manifest = verify_manifest

    def __call__(self, context: PhaseExecutionContext) -> BacktestDeterminismProof:
        del context
        left = _resolved_result_root(self._left, "left")
        right = _resolved_result_root(self._right, "right")
        try:
            if left.resolve(strict=True) == right.resolve(strict=True):
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.PHASE_BLOCKED,
                    "determinism requires two distinct durable Backtest result roots",
                )
        except OSError as exc:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest determinism result root is unavailable",
            ) from exc
        return BacktestDeterminismProof(
            left_artifact_digest=_verified_manifest_digest(left, self._verify_manifest),
            right_artifact_digest=_verified_manifest_digest(right, self._verify_manifest),
        )


def _outcome_identity(outcome: BacktestRunOutcomePort) -> tuple[Path, str, str, bool]:
    output_directory = outcome.output_directory
    run_id = outcome.run_id
    application_run_id = outcome.application_run_id
    restart_verified = outcome.restart_verified
    if not isinstance(output_directory, Path):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Backtest outcome directory is invalid",
        )
    run_id = _required_identifier(run_id, "Backtest outcome run_id")
    if application_run_id is None:
        application_run_id = run_id
    application_run_id = _required_identifier(
        application_run_id,
        "Backtest outcome application_run_id",
    )
    if not isinstance(restart_verified, bool):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Backtest outcome restart status is invalid",
        )
    return output_directory, run_id, application_run_id, restart_verified


def _resolved_result_root(resolver: BacktestResultResolver, label: str) -> Path:
    try:
        path = resolver()
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            f"{label} Backtest result resolver failed",
        ) from exc
    if not isinstance(path, Path):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            f"{label} Backtest result resolver returned an invalid path",
        )
    return path


def _verified_manifest_digest(
    directory: Path,
    verifier: BacktestManifestVerifier,
    *,
    expected_run_id: str | None = None,
    expected_application_run_id: str | None = None,
) -> str:
    if directory.is_symlink() or not directory.is_dir():
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest result root must be a real directory",
        )
    try:
        manifest = verifier(directory)
    except AcceptanceOwnerError:
        raise
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest artifact manifest verification failed",
        ) from exc
    if not isinstance(manifest, Mapping):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest manifest verifier returned an invalid mapping",
        )
    run_id = _required_identifier(
        manifest.get("run_id"),
        "verified Backtest manifest run_id",
    )
    if expected_run_id is not None and run_id != expected_run_id:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest manifest run_id differs from authoritative run outcome",
        )
    manifest_application_id = manifest.get("application_run_id")
    if expected_application_run_id is not None:
        expected_manifest_application_id = (
            None if expected_application_run_id == run_id else expected_application_run_id
        )
        if manifest_application_id != expected_manifest_application_id:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest manifest application_run_id differs from authoritative run outcome",
            )
    files = manifest.get("files")
    if not isinstance(files, Mapping) or not files:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "verified Backtest manifest contains no artifact files",
        )
    try:
        encoded = json.dumps(
            dict(manifest),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "verified Backtest manifest is not canonical JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _read_application_run(directory: Path) -> dict[str, object]:
    path = directory / "application-run.json"
    if path.is_symlink() or not path.is_file():
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "verified Backtest result is missing application-run.json",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest application-run evidence is unavailable",
        ) from exc
    if not raw or len(raw) > _MAX_APPLICATION_RUN_BYTES:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest application-run evidence has an invalid size",
        )
    try:
        payload: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest application-run evidence is invalid JSON",
        ) from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest application-run evidence must be a string-keyed object",
        )
    return cast(dict[str, object], payload)


def _require_application_identity(
    application: Mapping[str, object],
    *,
    run_id: str,
    application_run_id: str,
) -> None:
    core_run_id = _required_identifier(
        application.get("core_run_id"),
        "Backtest application core_run_id",
    )
    observed_application_id = _required_identifier(
        application.get("application_run_id"),
        "Backtest application application_run_id",
    )
    if core_run_id != run_id or observed_application_id != application_run_id:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest outcome identity differs from verified application artifacts",
        )


def _required_identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{name} is invalid",
        )
    return require_identifier(value, name)


def _optional_digest(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"Backtest application {name} is invalid",
        )
    return require_sha256(value, f"Backtest application {name}")


__all__ = [
    "BacktestManifestVerifier",
    "BacktestResultResolver",
    "BacktestRunOutcomePort",
    "BacktestRunOwner",
    "VerifiedBacktestDeterminismBinding",
    "VerifiedBacktestRunBinding",
]
