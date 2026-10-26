"""Factory-owned construction of the independent Strategy Auditor evidence envelope."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .canonical import canonical_sha256
from .contracts import (
    CandidateStrategy,
    DatasetDescriptor,
    HoldoutReport,
    MarketType,
    RobustnessReport,
    StrategyFactoryManifest,
    StrategyFactoryRequest,
    TimeRange,
)
from .errors import CompatibilityError, ExternalComponentUnavailableError

if TYPE_CHECKING:
    from .run import StrategyFactoryRun

AUDIT_CONTRACT_VERSION = "strategy-audit-port-v1"
AUDIT_CONTRACT_VERSION_V2 = "strategy-audit-port-v2"
EVIDENCE_REFERENCE_PREFIX = "strategy-factory-artifact:"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_KEYS = {
    "schema_version",
    "package_id",
    "created_at",
    "candidate",
    "dataset_lineage",
    "research",
    "robustness",
    "execution_assumptions",
    "risk_policy",
}
_ENVELOPE_V2_KEYS = _ENVELOPE_KEYS | {"package_intent"}
_FUTURES = frozenset({MarketType.LINEAR_FUTURES, MarketType.INVERSE_FUTURES})


@dataclass(frozen=True, slots=True)
class DatasetAuditLineage:
    """Authoritative immutable Dataset Registry evidence required by the auditor."""

    dataset_id: str
    registry_version: str
    instrument: str
    exchange: str
    market_type: str
    manifest_sha256: str
    dataset_sha256: str
    timeframes: tuple[str, ...]
    immutable: bool
    tamper_verified: bool
    funding_lineage_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "dataset_id",
            "registry_version",
            "instrument",
            "exchange",
            "market_type",
        ):
            _nonblank(getattr(self, name), name)
        _digest(self.manifest_sha256, "manifest_sha256")
        _digest(self.dataset_sha256, "dataset_sha256")
        normalized = tuple(dict.fromkeys(self.timeframes))
        if not normalized or any(not isinstance(item, str) or not item for item in normalized):
            raise CompatibilityError("dataset audit timeframes must be non-empty strings")
        if len(normalized) != len(self.timeframes):
            raise CompatibilityError("dataset audit timeframes must be unique")
        object.__setattr__(self, "timeframes", normalized)
        if self.immutable is not True or self.tamper_verified is not True:
            raise CompatibilityError("dataset audit lineage must be immutable and tamper-verified")
        if self.funding_lineage_sha256 is not None:
            _digest(self.funding_lineage_sha256, "funding_lineage_sha256")

    def as_mapping(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "registry_version": self.registry_version,
            "instrument": self.instrument,
            "exchange": self.exchange,
            "market_type": self.market_type,
            "manifest_sha256": self.manifest_sha256,
            "dataset_sha256": self.dataset_sha256,
            "timeframes": self.timeframes,
            "immutable": True,
            "tamper_verified": True,
        }
        if self.funding_lineage_sha256 is not None:
            payload["funding_lineage_sha256"] = self.funding_lineage_sha256
        return MappingProxyType(payload)


@dataclass(frozen=True, slots=True)
class ResultsAuditEvidence:
    """Authoritative Results Aggregator package identities for one candidate."""

    candidate_id: str
    configuration_hash: str
    experiment_ids: tuple[str, ...]
    registry_package_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonblank(self.candidate_id, "results candidate_id")
        _digest(self.configuration_hash, "results configuration_hash")
        experiments = tuple(sorted(set(self.experiment_ids)))
        packages = tuple(sorted(set(self.registry_package_hashes)))
        if not experiments or any(not isinstance(item, str) or not item for item in experiments):
            raise CompatibilityError("Results evidence requires experiment identities")
        if not packages:
            raise CompatibilityError("Results evidence requires registry package hashes")
        for digest in packages:
            _digest(digest, "Results registry package hash")
        object.__setattr__(self, "experiment_ids", experiments)
        object.__setattr__(self, "registry_package_hashes", packages)


@runtime_checkable
class DatasetAuditLineageSource(Protocol):
    """Read-only authoritative Dataset Registry boundary."""

    def lineage_for(self, dataset: DatasetDescriptor) -> DatasetAuditLineage: ...


@runtime_checkable
class ResultsAuditEvidenceSource(Protocol):
    """Read-only authoritative Results Aggregator boundary."""

    def evidence_for(
        self,
        candidate: CandidateStrategy,
        *,
        experiment_id: str,
    ) -> ResultsAuditEvidence: ...


@dataclass(frozen=True, slots=True)
class AuditResearchPeriods:
    train: TimeRange
    validation: TimeRange
    test: TimeRange
    holdout: TimeRange

    def __post_init__(self) -> None:
        ranges = (self.train, self.validation, self.test, self.holdout)
        if not all(
            left.end <= right.start for left, right in zip(ranges, ranges[1:], strict=False)
        ):
            raise CompatibilityError("audit research periods overlap or are out of order")

    def as_mapping(self) -> Mapping[str, Mapping[str, datetime]]:
        return MappingProxyType(
            {
                "train": _period(self.train),
                "validation": _period(self.validation),
                "test": _period(self.test),
                "holdout": _period(self.holdout),
            }
        )


class FactoryAuditEnvelopeBuilder:
    """Build the exact immutable envelope accepted by ``strategy-audit-port-v1``."""

    def __init__(
        self,
        *,
        dataset_source: DatasetAuditLineageSource,
        results_source: ResultsAuditEvidenceSource,
        contract_version: str = AUDIT_CONTRACT_VERSION,
    ) -> None:
        if not isinstance(dataset_source, DatasetAuditLineageSource):
            raise TypeError("dataset_source must implement DatasetAuditLineageSource")
        if not isinstance(results_source, ResultsAuditEvidenceSource):
            raise TypeError("results_source must implement ResultsAuditEvidenceSource")
        self._datasets = dataset_source
        self._results = results_source
        if contract_version not in {AUDIT_CONTRACT_VERSION, AUDIT_CONTRACT_VERSION_V2}:
            raise ValueError("unsupported Factory audit contract version")
        self.contract_version = contract_version

    def build(
        self,
        *,
        request: StrategyFactoryRequest,
        manifest: StrategyFactoryManifest,
        candidate: CandidateStrategy,
        robustness: RobustnessReport,
        holdout: HoldoutReport,
        periods: AuditResearchPeriods,
        experiment_id: str,
        package_intent: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        _nonblank(experiment_id, "experiment_id")
        if robustness.candidate_id != candidate.candidate_id:
            raise CompatibilityError("robustness evidence belongs to another candidate")
        if holdout.candidate_id != candidate.candidate_id:
            raise CompatibilityError("holdout evidence belongs to another candidate")
        if holdout.period != periods.holdout:
            raise CompatibilityError("holdout report period differs from the research split")
        if not holdout.opened_once:
            raise CompatibilityError("holdout evidence must prove one-time opening")

        lineage = self._datasets.lineage_for(request.dataset)
        self._validate_lineage(request, lineage)
        results = self._results.evidence_for(candidate, experiment_id=experiment_id)
        self._validate_results(candidate, experiment_id, results)

        candidate_payload = {
            "candidate_id": candidate.candidate_id,
            "configuration_hash": candidate.configuration_hash,
            "parameters": candidate.parameters,
            "live_trading_authorized": False,
        }
        created_at = max(
            manifest.created_at,
            robustness.generated_at,
            holdout.generated_at,
        ).astimezone(UTC)
        if self.contract_version == AUDIT_CONTRACT_VERSION_V2:
            from .package_intent import request_context, verify_intent_mapping

            if package_intent is None:
                raise CompatibilityError("v2 audit envelope requires PackageIntent")
            verify_intent_mapping(package_intent)
            if package_intent.get("request_fingerprint") != request.fingerprint or dict(
                package_intent.get("request_context", {})
            ) != dict(request_context(request)):
                raise CompatibilityError("PackageIntent differs from audited Factory request")
        elif package_intent is not None:
            raise CompatibilityError("v1 audit envelope cannot include PackageIntent")

        body: dict[str, Any] = {
            "schema_version": 2 if self.contract_version == AUDIT_CONTRACT_VERSION_V2 else 1,
            "created_at": created_at,
            "candidate": candidate_payload,
            "dataset_lineage": lineage.as_mapping(),
            "research": {
                "candidate_id": candidate.candidate_id,
                "configuration_hash": candidate.configuration_hash,
                "periods": periods.as_mapping(),
                "holdout_isolated": True,
                "holdout_opened_once": holdout.opened_once,
                "random_seeds": (request.random_seed,),
                "results_evidence": {
                    "registry_package_hashes": results.registry_package_hashes,
                },
            },
            "robustness": {
                "candidate_id": candidate.candidate_id,
                "report_sha256": canonical_sha256(robustness),
                "passed": robustness.passed,
            },
            "execution_assumptions": {
                "mode": "paper",
                "exchange": request.exchange,
                "market_type": request.market_type.value,
                "live_trading_authorized": False,
                "private_exchange_access": False,
                "order_placement": False,
            },
            "risk_policy": {
                "paper_only": True,
                "maximum_risk_fraction": format(request.research_risk_fraction, "f"),
                "maximum_drawdown_fraction": format(
                    request.maximum_drawdown_fraction,
                    "f",
                ),
            },
        }
        if package_intent is not None:
            body["package_intent"] = package_intent
        package_seed = {
            "factory_run_id": manifest.run_id,
            "factory_manifest_id": manifest.manifest_id,
            "factory_commit_sha": manifest.commit_sha,
            **body,
        }
        body["package_id"] = f"sf-audit-{canonical_sha256(package_seed)[:24]}"
        expected_keys = (
            _ENVELOPE_V2_KEYS
            if self.contract_version == AUDIT_CONTRACT_VERSION_V2
            else _ENVELOPE_KEYS
        )
        if set(body) != expected_keys:
            raise CompatibilityError("Factory audit envelope fields differ from contract")
        return MappingProxyType(body)

    @staticmethod
    def _validate_lineage(
        request: StrategyFactoryRequest,
        lineage: DatasetAuditLineage,
    ) -> None:
        dataset = request.dataset
        if lineage.dataset_id != dataset.dataset_id:
            raise CompatibilityError("Dataset Registry lineage belongs to another dataset")
        if lineage.dataset_sha256 != dataset.sha256:
            raise CompatibilityError("Dataset Registry hash differs from Factory dataset")
        if lineage.instrument != request.symbol:
            raise CompatibilityError("Dataset Registry instrument differs from Factory request")
        if lineage.exchange != request.exchange:
            raise CompatibilityError("Dataset Registry exchange differs from Factory request")
        if lineage.market_type != request.market_type.value:
            raise CompatibilityError("Dataset Registry market type differs from Factory request")
        if not set(request.allowed_timeframes).issubset(lineage.timeframes):
            raise CompatibilityError("Dataset Registry lineage lacks requested timeframes")
        if request.market_type in _FUTURES and lineage.funding_lineage_sha256 is None:
            raise CompatibilityError("futures audit evidence requires funding lineage")

    @staticmethod
    def _validate_results(
        candidate: CandidateStrategy,
        experiment_id: str,
        results: ResultsAuditEvidence,
    ) -> None:
        if results.candidate_id != candidate.candidate_id:
            raise CompatibilityError("Results evidence belongs to another candidate")
        if results.configuration_hash != candidate.configuration_hash:
            raise CompatibilityError("Results configuration hash differs from candidate")
        if experiment_id not in results.experiment_ids:
            raise CompatibilityError("Results evidence does not contain the selected experiment")


class FactoryArtifactEvidenceResolver:
    """Resolve only content-addressed Factory audit envelopes for the independent port."""

    def __init__(self, run: StrategyFactoryRun) -> None:
        if not callable(getattr(run, "load_artifact", None)):
            raise TypeError("run must expose load_artifact()")
        self._run = run

    def __call__(self, evidence_package_reference: str) -> Mapping[str, Any]:
        if not isinstance(
            evidence_package_reference, str
        ) or not evidence_package_reference.startswith(EVIDENCE_REFERENCE_PREFIX):
            raise CompatibilityError("unsupported Factory audit evidence reference")
        artifact_id = evidence_package_reference.removeprefix(EVIDENCE_REFERENCE_PREFIX)
        if not artifact_id.startswith("strategy_audit_envelope-"):
            raise CompatibilityError("Factory audit reference is not an envelope artifact")
        payload = self._run.load_artifact(artifact_id)
        if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
            raise CompatibilityError("Factory audit envelope artifact is malformed")
        payload_keys = set(payload)
        if payload_keys != _ENVELOPE_KEYS and payload_keys != _ENVELOPE_V2_KEYS:
            raise CompatibilityError("Factory audit envelope fields differ from contract")
        return MappingProxyType(dict(payload))


class UnavailableDatasetAuditLineageSource:
    def lineage_for(self, dataset: DatasetDescriptor) -> DatasetAuditLineage:
        del dataset
        raise ExternalComponentUnavailableError("Dataset Registry audit lineage is unavailable")


class UnavailableResultsAuditEvidenceSource:
    def evidence_for(
        self,
        candidate: CandidateStrategy,
        *,
        experiment_id: str,
    ) -> ResultsAuditEvidence:
        del candidate, experiment_id
        raise ExternalComponentUnavailableError("Results Aggregator audit packages are unavailable")


def _period(value: TimeRange) -> Mapping[str, datetime]:
    return MappingProxyType({"start": value.start, "end": value.end})


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CompatibilityError(f"{name} must be a lowercase SHA-256")
    return value


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CompatibilityError(f"{name} must be a non-blank trimmed string")
    return value


__all__ = [
    "AUDIT_CONTRACT_VERSION",
    "AUDIT_CONTRACT_VERSION_V2",
    "EVIDENCE_REFERENCE_PREFIX",
    "AuditResearchPeriods",
    "DatasetAuditLineage",
    "DatasetAuditLineageSource",
    "FactoryArtifactEvidenceResolver",
    "FactoryAuditEnvelopeBuilder",
    "ResultsAuditEvidence",
    "ResultsAuditEvidenceSource",
    "UnavailableDatasetAuditLineageSource",
    "UnavailableResultsAuditEvidenceSource",
]
