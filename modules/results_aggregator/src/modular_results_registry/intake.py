"""Compatibility facade for artifact-package intake."""

from .intake_manifest import manifest_from_mapping
from .intake_models import (
    ArtifactInventoryItem,
    ArtifactPackageManifest,
    IntakeLimits,
    LegacyFileRecord,
    LegacyImportReport,
    ProducerProfile,
    VerifiedArtifactPackage,
)
from .intake_validation import inspect_legacy_directory, validate_artifact_package

__all__ = [
    "ArtifactInventoryItem",
    "ArtifactPackageManifest",
    "IntakeLimits",
    "LegacyFileRecord",
    "LegacyImportReport",
    "ProducerProfile",
    "VerifiedArtifactPackage",
    "inspect_legacy_directory",
    "manifest_from_mapping",
    "validate_artifact_package",
]
