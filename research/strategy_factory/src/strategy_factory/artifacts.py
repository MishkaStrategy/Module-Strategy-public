"""Content-addressed immutable artifact persistence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .contract_codec import decode_strategy_package
from .contracts import AdmissionStatus, StrategyPackage
from .errors import CheckpointError
from .package import StrategyPackageBuilder


class ContentAddressedArtifactStore:
    """Persist immutable canonical JSON artifacts without overwriting evidence."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, *, artifact_type: str, value: Any) -> str:
        if not artifact_type or not artifact_type.replace("-", "").replace("_", "").isalnum():
            raise CheckpointError("artifact_type must be portable")
        payload_hash = canonical_sha256(value)
        artifact_id = f"{artifact_type}-{payload_hash}"
        directory = self._root / artifact_type
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{payload_hash}.json"
        raw = canonical_json_bytes(
            {
                "artifact_type": artifact_type,
                "artifact_id": artifact_id,
                "payload_hash": payload_hash,
                "payload": value,
            }
        )
        if target.exists():
            try:
                if target.read_bytes() != raw:
                    raise CheckpointError("artifact hash collision or storage corruption")
            except OSError as exc:
                raise CheckpointError("cannot verify existing artifact") from exc
            return artifact_id
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != raw:
                    raise CheckpointError("artifact identity conflict") from None
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CheckpointError("cannot persist artifact") from exc
        return artifact_id

    def get(self, artifact_id: str) -> Any:
        """Read and verify an immutable artifact by content-addressed identity."""

        target = self.path_for(artifact_id)
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise CheckpointError(f"cannot read artifact: {artifact_id}") from exc
        try:
            value = strict_json_loads(raw)
        except Exception as exc:
            raise CheckpointError(f"artifact JSON is invalid: {artifact_id}") from exc
        if not isinstance(value, dict):
            raise CheckpointError("artifact envelope must be an object")
        required = {"artifact_type", "artifact_id", "payload_hash", "payload"}
        if set(value) != required:
            raise CheckpointError("artifact envelope fields are invalid")
        artifact_type, digest = self._identity(artifact_id)
        if value["artifact_type"] != artifact_type or value["artifact_id"] != artifact_id:
            raise CheckpointError("artifact envelope identity mismatch")
        if value["payload_hash"] != digest:
            raise CheckpointError("artifact envelope hash mismatch")
        payload = value["payload"]
        if canonical_sha256(payload) != digest:
            raise CheckpointError("artifact payload hash mismatch")
        return payload

    def exists(self, artifact_id: str) -> bool:
        """Return whether a syntactically valid artifact identity exists on disk."""

        return self.path_for(artifact_id).is_file()

    def path_for(self, artifact_id: str) -> Path:
        artifact_type, digest = self._identity(artifact_id)
        return self._root / artifact_type / f"{digest}.json"

    @staticmethod
    def _identity(artifact_id: str) -> tuple[str, str]:
        if not isinstance(artifact_id, str) or not artifact_id:
            raise CheckpointError("invalid artifact_id")
        try:
            artifact_type, digest = artifact_id.rsplit("-", 1)
        except ValueError as exc:
            raise CheckpointError("invalid artifact_id") from exc
        if (
            not artifact_type
            or not artifact_type.replace("-", "").replace("_", "").isalnum()
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CheckpointError("invalid artifact identity")
        return artifact_type, digest


class ApprovedStrategyPackageLoader:
    """Load one configured PAPER-approved StrategyPackage from its owner store.

    The loader has no caller-selected identity at load time.  Both the immutable
    artifact identity and the expected semantic identity are fixed when the trusted
    service-manager composition is constructed.
    """

    def __init__(
        self,
        store: ContentAddressedArtifactStore,
        *,
        artifact_id: str,
        strategy_id: str,
        package_hash: str,
    ) -> None:
        if not isinstance(store, ContentAddressedArtifactStore):
            raise TypeError("store must be ContentAddressedArtifactStore")
        artifact_type, _ = store._identity(artifact_id)
        if artifact_type != "strategy_package":
            raise CheckpointError("configured artifact is not a Strategy Package")
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise CheckpointError("configured strategy_id is invalid")
        if (
            not isinstance(package_hash, str)
            or len(package_hash) != 64
            or any(character not in "0123456789abcdef" for character in package_hash)
        ):
            raise CheckpointError("configured package_hash is invalid")
        self._store = store
        self._artifact_id = artifact_id
        self._strategy_id = strategy_id
        self._package_hash = package_hash

    def load(self) -> StrategyPackage:
        raw = self._store.get(self._artifact_id)
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "strategy_package",
            "admission_status",
            "audit_decision_hash",
        }:
            raise CheckpointError("Strategy Package artifact schema is invalid")
        if raw["schema_version"] != 1:
            raise CheckpointError("Strategy Package artifact schema is unsupported")
        try:
            package = decode_strategy_package(raw["strategy_package"])
            StrategyPackageBuilder().verify(package)
        except Exception as exc:
            raise CheckpointError("Strategy Package reconstruction failed") from exc
        if package.strategy_id != self._strategy_id:
            raise CheckpointError("Strategy Package strategy identity mismatch")
        if package.package_hash != self._package_hash:
            raise CheckpointError("Strategy Package hash identity mismatch")
        if raw["audit_decision_hash"] != package.constraints.get("audit_decision_hash"):
            raise CheckpointError("Strategy Package audit decision identity mismatch")
        if (
            raw["admission_status"] != AdmissionStatus.APPROVED_FOR_PAPER.value
            or package.admission_status is not AdmissionStatus.APPROVED_FOR_PAPER
        ):
            raise CheckpointError("Strategy Package is not approved for PAPER")
        if package.live_trading_authorized:
            raise CheckpointError("Strategy Package must not authorize LIVE trading")
        return package
