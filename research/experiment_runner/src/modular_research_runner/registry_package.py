from __future__ import annotations

from pathlib import Path
from typing import Any

from . import _registry_package_v4 as _impl
from .futures_evidence import build_futures_producer_assumptions

# Preserve the current v4/sharded-event implementation and extend only the
# manifest-declared optional producer evidence admitted by #459. The private
# implementation's literal tuple is intentionally widened at this compatibility
# boundary; runtime behavior remains the same tuple-of-file-names contract.
_impl._OPTIONAL_FILES = _impl._OPTIONAL_FILES + (  # type: ignore[assignment]
    "funding-runtime.json",
    "valuation-runtime.json",
)
_impl._ENGINE_MEDIA_TYPES = {
    **_impl._ENGINE_MEDIA_TYPES,
    "funding-runtime.json": "application/json",
    "valuation-runtime.json": "application/json",
}

RegistryPackageReference = _impl.RegistryPackageReference
RegistryPackageSink = _impl.RegistryPackageSink
_ENGINE_MANIFEST_FILENAME = _impl._ENGINE_MANIFEST_FILENAME
_REQUIRED_FILES = _impl._REQUIRED_FILES
_EVENT_STREAM_FILES = _impl._EVENT_STREAM_FILES
_SHARDED_EVENT_MANIFEST = _impl._SHARDED_EVENT_MANIFEST
_SHARDED_EVENT_PREFIX = _impl._SHARDED_EVENT_PREFIX
_SHARDED_EVENT_ENCODING = _impl._SHARDED_EVENT_ENCODING
_OPTIONAL_FILES = _impl._OPTIONAL_FILES
_ENGINE_MEDIA_TYPES = _impl._ENGINE_MEDIA_TYPES


class LocalResultsAggregatorPackageSink(_impl.LocalResultsAggregatorPackageSink):
    """Current registry-package sink with fail-closed futures producer-evidence validation."""

    def _inventory(
        self,
        source_root: Path,
        *,
        engine_version: str,
        expected_engine_run_id: str,
    ) -> list[dict[str, Any]]:
        inventory = super()._inventory(
            source_root,
            engine_version=engine_version,
            expected_engine_run_id=expected_engine_run_id,
        )
        engine_manifest = _impl._load_engine_manifest(source_root)
        build_futures_producer_assumptions(source_root, engine_manifest)
        return inventory


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


__all__ = [
    "LocalResultsAggregatorPackageSink",
    "RegistryPackageReference",
    "RegistryPackageSink",
]
