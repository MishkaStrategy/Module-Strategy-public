"""Independent evidence validation and external verifier ports."""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

from .canonical import canonical_sha256
from .contracts import AuditOutcome, VerificationResult
from .errors import AuditUnavailableError, EvidenceMalformedError
from .policy import AuditPolicy

_SHA = re.compile(r"^[0-9a-f]{64}$")
_OBJECTIVE_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_AUDIT_V1_KEYS = {
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
_AUDIT_V2_KEYS = _AUDIT_V1_KEYS | {"package_intent"}


@runtime_checkable
class DatasetLineageVerifier(Protocol):
    def verify(self, lineage: Mapping[str, Any]) -> VerificationResult: ...


@runtime_checkable
class ResultsEvidenceVerifier(Protocol):
    def verify(self, evidence: Mapping[str, Any]) -> VerificationResult: ...


class UnavailableDatasetLineageVerifier:
    def verify(self, lineage: Mapping[str, Any]) -> VerificationResult:
        del lineage
        raise AuditUnavailableError("Dataset Registry verifier is unavailable")


class UnavailableResultsEvidenceVerifier:
    def verify(self, evidence: Mapping[str, Any]) -> VerificationResult:
        del evidence
        raise AuditUnavailableError("Results Aggregator verifier is unavailable")


class StaticDigestDatasetVerifier:
    """Small adapter for tests and air-gapped staging manifests."""

    def __init__(self, expected_manifest_hashes: set[str]) -> None:
        self._expected = frozenset(expected_manifest_hashes)

    def verify(self, lineage: Mapping[str, Any]) -> VerificationResult:
        manifest = _sha(lineage.get("manifest_sha256"), "dataset manifest_sha256")
        verified = manifest in self._expected
        return VerificationResult(
            verified=verified,
            reason_codes=(
                "DATASET_REGISTRY_MANIFEST_VERIFIED"
                if verified
                else "DATASET_REGISTRY_MANIFEST_UNKNOWN",
            ),
            evidence={"manifest_sha256": manifest},
        )


class StaticDigestResultsVerifier:
    def __init__(self, expected_package_hashes: set[str]) -> None:
        self._expected = frozenset(expected_package_hashes)

    def verify(self, evidence: Mapping[str, Any]) -> VerificationResult:
        packages = _sequence(evidence.get("registry_package_hashes"), "registry_package_hashes")
        hashes = tuple(_sha(item, "registry package hash") for item in packages)
        verified = bool(hashes) and set(hashes).issubset(self._expected)
        return VerificationResult(
            verified=verified,
            reason_codes=(
                "RESULTS_REGISTRY_PACKAGES_VERIFIED"
                if verified
                else "RESULTS_REGISTRY_PACKAGES_UNKNOWN",
            ),
            evidence={"registry_package_hashes": hashes},
        )


class EvidenceValidator:
    def __init__(
        self,
        *,
        policy: AuditPolicy,
        dataset_verifier: DatasetLineageVerifier,
        results_verifier: ResultsEvidenceVerifier,
    ) -> None:
        self._policy = policy
        self._dataset = dataset_verifier
        self._results = results_verifier

    def evaluate(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> tuple[str, AuditOutcome, tuple[str, ...]]:
        schema_version = payload.get("schema_version")
        expected_keys = _AUDIT_V2_KEYS if schema_version == 2 else _AUDIT_V1_KEYS
        _exact_keys(payload, expected_keys, "audit package")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, 2}
        ):
            raise EvidenceMalformedError("unsupported audit package schema_version")
        _identifier(payload["package_id"], "package_id")
        now_utc = _utc(now, "now")
        created = _datetime(payload["created_at"], "created_at")
        age = (now_utc - created).total_seconds()
        if age < 0:
            raise EvidenceMalformedError("audit package is future-dated")

        candidate = _mapping(payload["candidate"], "candidate")
        candidate_id = _identifier(candidate.get("candidate_id"), "candidate_id")
        configuration_hash = _sha(candidate.get("configuration_hash"), "configuration_hash")
        parameters = _mapping(candidate.get("parameters"), "candidate.parameters")
        if canonical_sha256(parameters) != configuration_hash:
            raise EvidenceMalformedError("candidate configuration hash mismatch")
        if candidate.get("live_trading_authorized", False) is not False:
            raise EvidenceMalformedError("candidate attempts live authorization")

        reasons: list[str] = ["CANDIDATE_IDENTITY_VERIFIED", "CONFIGURATION_HASH_VERIFIED"]
        research_only = False
        if age > self._policy.maximum_evidence_age_seconds:
            reasons.extend(("EVIDENCE_STALE", "PAPER_ADMISSION_REJECTED"))
            return candidate_id, AuditOutcome.REJECTED, tuple(sorted(set(reasons)))
        reasons.append("EVIDENCE_FRESH")

        lineage = _mapping(payload["dataset_lineage"], "dataset_lineage")
        self._validate_lineage(lineage)
        dataset_result = self._dataset.verify(lineage)
        reasons.extend(dataset_result.reason_codes)
        if not dataset_result.verified:
            research_only = True

        research = _mapping(payload["research"], "research")
        self._validate_research(research, candidate_id, configuration_hash)
        results_evidence = _mapping(research.get("results_evidence"), "research.results_evidence")
        results_result = self._results.verify(results_evidence)
        reasons.extend(results_result.reason_codes)
        if not results_result.verified:
            research_only = True

        robustness = _mapping(payload["robustness"], "robustness")
        if _identifier(robustness.get("candidate_id"), "robustness.candidate_id") != candidate_id:
            raise EvidenceMalformedError("robustness belongs to another candidate")
        _sha(robustness.get("report_sha256"), "robustness.report_sha256")
        if robustness.get("passed") is not True:
            reasons.append("ROBUSTNESS_NOT_PASSED")
            research_only = True
        else:
            reasons.append("ROBUSTNESS_PASSED")

        execution = _mapping(payload["execution_assumptions"], "execution_assumptions")
        self._validate_execution(execution)
        reasons.append("PAPER_ONLY_EXECUTION_ASSUMPTIONS_VERIFIED")

        risk = _mapping(payload["risk_policy"], "risk_policy")
        if not self._validate_risk(risk):
            reasons.append("RISK_POLICY_OUTSIDE_AUDITOR_LIMITS")
            research_only = True
        else:
            reasons.append("RISK_POLICY_VERIFIED")

        if schema_version == 2:
            self._validate_package_intent(
                payload["package_intent"],
                candidate_id=candidate_id,
                configuration_hash=configuration_hash,
                expected_robustness_hash=_sha(
                    robustness.get("report_sha256"), "robustness.report_sha256"
                ),
                envelope_risk=risk,
                envelope_execution=execution,
                envelope_research=research,
                envelope_lineage=lineage,
                envelope_created=created,
            )
            # Current Results and Holdout ports expose package/report identities,
            # not independently verified metric-value manifests. A signed intent
            # binds claims but cannot elevate them to source evidence.
            reasons.extend(
                (
                    "PACKAGE_INTENT_CONTENT_BOUND",
                    "RESULT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE",
                    "HOLDOUT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE",
                )
            )
            research_only = True

        if research_only:
            reasons.append("PAPER_ADMISSION_WITHHELD")
            return candidate_id, AuditOutcome.RESEARCH_ONLY, tuple(sorted(set(reasons)))
        reasons.append("ALL_INDEPENDENT_GATES_PASSED")
        return candidate_id, AuditOutcome.APPROVED_FOR_PAPER, tuple(sorted(set(reasons)))

    @staticmethod
    def _validate_package_intent(
        raw_intent: object,
        *,
        candidate_id: str,
        configuration_hash: str,
        expected_robustness_hash: str,
        envelope_risk: Mapping[str, Any],
        envelope_execution: Mapping[str, Any],
        envelope_research: Mapping[str, Any],
        envelope_lineage: Mapping[str, Any],
        envelope_created: datetime,
    ) -> None:
        intent = _mapping(raw_intent, "package_intent")
        _exact_keys(
            intent,
            {
                "schema_version",
                "request_fingerprint",
                "request_context",
                "package_projection",
                "intent_hash",
            },
            "package_intent",
        )
        intent_schema = intent.get("schema_version")
        if (
            isinstance(intent_schema, bool)
            or not isinstance(intent_schema, int)
            or intent_schema != 1
        ):
            raise EvidenceMalformedError("unsupported package intent schema_version")
        request_fingerprint = _sha(
            intent.get("request_fingerprint"), "package_intent.request_fingerprint"
        )
        request_context = _mapping(intent.get("request_context"), "package_intent.request_context")
        request_keys = {
            "request_id",
            "mode",
            "symbol",
            "exchange",
            "market_type",
            "dataset",
            "allowed_timeframes",
            "initial_capital",
            "research_risk_fraction",
            "maximum_drawdown_fraction",
            "minimum_trade_count",
            "objectives",
            "budget",
            "random_seed",
            "allowed_strategy_families",
            "forbidden_module_ids",
            "baseline_strategy_id",
            "source_strategy_id",
            "adaptation_policy",
            "improvement_policy",
            "explicit_holdout",
            "created_at",
            "schema_version",
        }
        _exact_keys(request_context, request_keys, "package_intent.request_context")
        request_created = _validate_v2_request_context(request_context)
        if request_created > envelope_created:
            raise EvidenceMalformedError("Factory request was created after audit evidence")
        if canonical_sha256(request_context) != request_fingerprint:
            raise EvidenceMalformedError("package intent request fingerprint mismatch")
        request_dataset = _mapping(request_context.get("dataset"), "request dataset")
        if (
            request_dataset.get("dataset_id") != envelope_lineage.get("dataset_id")
            or request_dataset.get("sha256") != envelope_lineage.get("dataset_sha256")
            or request_dataset.get("symbol") != envelope_lineage.get("instrument")
            or request_dataset.get("exchange") != envelope_lineage.get("exchange")
            or request_dataset.get("market_type") != envelope_lineage.get("market_type")
        ):
            raise EvidenceMalformedError("request dataset identity differs from audit lineage")
        if not set(request_context["allowed_timeframes"]).issubset(
            set(envelope_lineage["timeframes"])
        ):
            raise EvidenceMalformedError("audit lineage lacks requested dataset timeframes")
        _validate_v2_objectives(request_context.get("objectives"), request_context=request_context)
        projection = _mapping(intent.get("package_projection"), "package_intent.package_projection")
        projection_keys = {
            "schema_version",
            "request_context",
            "strategy_id",
            "version",
            "symbol",
            "exchange",
            "market_type",
            "directions",
            "architecture",
            "parameters",
            "risk_policy",
            "constraints",
            "timeframes",
            "execution_assumptions",
            "dataset_hashes",
            "research_periods",
            "commit_sha",
            "random_seeds",
            "key_results",
            "robustness_report",
            "holdout_report",
            "known_weaknesses",
            "disabled_regimes",
            "evidence_links",
            "paper_configuration",
        }
        _exact_keys(projection, projection_keys, "package_intent.package_projection")
        projection_schema = projection.get("schema_version")
        if (
            isinstance(projection_schema, bool)
            or not isinstance(projection_schema, int)
            or projection_schema != 1
        ):
            raise EvidenceMalformedError("unsupported package projection schema_version")
        if dict(projection.get("request_context", {})) != dict(request_context):
            raise EvidenceMalformedError("package projection request context mismatch")
        constraints = _mapping(projection.get("constraints"), "package projection constraints")
        if (
            constraints.get("candidate_id") != candidate_id
            or constraints.get("candidate_configuration_hash") != configuration_hash
            or constraints.get("factory_request_fingerprint") != request_fingerprint
        ):
            raise EvidenceMalformedError("package intent candidate lineage mismatch")
        if (
            projection.get("symbol") != request_context.get("symbol")
            or projection.get("exchange") != request_context.get("exchange")
            or projection.get("market_type") != request_context.get("market_type")
        ):
            raise EvidenceMalformedError("package intent instrument differs from request")
        if tuple(projection.get("timeframes", ())) != tuple(request_context["allowed_timeframes"]):
            raise EvidenceMalformedError("package timeframes differ from Factory request")
        if tuple(projection.get("random_seeds", ())) != (request_context["random_seed"],):
            raise EvidenceMalformedError("package random seed differs from Factory request")
        datasets = _sequence(projection.get("dataset_hashes"), "package dataset hashes")
        expected_dataset_hashes = [request_dataset["sha256"]]
        for optional_hash in (
            request_dataset.get("funding_sha256"),
            request_dataset.get("exchange_info_sha256"),
        ):
            if optional_hash is not None:
                expected_dataset_hashes.append(optional_hash)
        if list(datasets) != expected_dataset_hashes:
            raise EvidenceMalformedError("package dataset hashes differ from request dataset")
        expected_version = request_context["objectives"].get("strategy_version", "0.1.0")
        if projection.get("version") != expected_version:
            raise EvidenceMalformedError("package version differs from Factory request")
        expected_strategy_id = (
            f"strategy-{str(request_context['symbol']).lower()}-{configuration_hash[:16]}"
        )
        if projection.get("strategy_id") != expected_strategy_id:
            raise EvidenceMalformedError("package identity differs from Factory request/candidate")
        if canonical_sha256(projection.get("robustness_report")) != expected_robustness_hash:
            raise EvidenceMalformedError("package intent robustness report hash mismatch")
        expected_intent_hash = canonical_sha256(
            {
                "schema_version": 1,
                "request_fingerprint": request_fingerprint,
                "request_context": request_context,
                "package_projection": projection,
            }
        )
        if _sha(intent.get("intent_hash"), "package_intent.intent_hash") != expected_intent_hash:
            raise EvidenceMalformedError("package intent hash mismatch")
        if not datasets or datasets[0] != request_dataset.get("sha256"):
            raise EvidenceMalformedError("package primary dataset differs from request")
        risk = _mapping(projection.get("risk_policy"), "package risk policy")
        execution = _mapping(projection.get("execution_assumptions"), "package execution")
        if set(risk) != {"research_risk_fraction", "initial_capital"}:
            raise EvidenceMalformedError(
                "package risk policy fields differ from supported v2 schema"
            )
        for field in ("research_risk_fraction", "initial_capital"):
            request_value = request_context.get(field)
            if isinstance(request_value, Mapping):
                request_value = request_value.get("$decimal")
            package_value = risk.get(field)
            if isinstance(package_value, Mapping):
                package_value = package_value.get("$decimal")
            try:
                if Decimal(str(package_value)) != Decimal(str(request_value)):
                    raise EvidenceMalformedError(f"package risk {field} differs from request")
            except InvalidOperation as exc:
                raise EvidenceMalformedError(f"package risk {field} is malformed") from exc
        if set(execution) != {"exchange", "market_type"}:
            raise EvidenceMalformedError("package execution fields differ from supported v2 schema")
        if execution.get("exchange") != request_context.get("exchange") or execution.get(
            "market_type"
        ) != request_context.get("market_type"):
            raise EvidenceMalformedError("package execution assumptions differ from request")
        for field in ("exchange", "market_type"):
            if envelope_execution.get(field) != execution.get(field):
                raise EvidenceMalformedError("audit execution summary differs from package intent")
        holdout = _mapping(projection.get("holdout_report"), "package holdout report")
        if (
            holdout.get("candidate_id") != candidate_id
            or holdout.get("opened_once") is not True
            or envelope_research.get("holdout_opened_once") is not True
        ):
            raise EvidenceMalformedError("package intent holdout proof is inconsistent")
        periods = _mapping(projection.get("research_periods"), "package research periods")
        dataset_period = _time_range(request_dataset.get("period"), "request dataset period")
        for name in ("train", "validation", "test", "holdout"):
            current_period = _time_range(periods.get(name), f"package research periods.{name}")
            if current_period[0] < dataset_period[0] or current_period[1] > dataset_period[1]:
                raise EvidenceMalformedError(
                    "package research period exceeds request dataset bounds"
                )
        holdout_period = periods.get("holdout")
        if not isinstance(holdout_period, Mapping) or holdout.get("period") != holdout_period:
            # JSON decoding preserves the exact canonical period mapping.
            try:
                if canonical_sha256(holdout.get("period")) != canonical_sha256(holdout_period):
                    raise EvidenceMalformedError("package intent holdout period mismatch")
            except (TypeError, ValueError) as exc:
                raise EvidenceMalformedError("package intent holdout period is malformed") from exc
        if canonical_sha256(envelope_research.get("periods")) != canonical_sha256(periods):
            raise EvidenceMalformedError("audit research periods differ from package intent")
        explicit_holdout = request_context.get("explicit_holdout")
        if explicit_holdout is not None and canonical_sha256(explicit_holdout) != canonical_sha256(
            holdout_period
        ):
            raise EvidenceMalformedError(
                "package holdout period differs from request explicit_holdout"
            )
        if tuple(envelope_research.get("random_seeds", ())) != tuple(
            projection.get("random_seeds", ())
        ):
            raise EvidenceMalformedError("audit random seeds differ from package intent")
        if constraints.get("minimum_trade_count") != request_context.get("minimum_trade_count"):
            raise EvidenceMalformedError("package minimum_trade_count differs from request")
        requested_weaknesses = set(request_context["objectives"].get("known_weaknesses", ()))
        if not requested_weaknesses.issubset(set(projection.get("known_weaknesses", ()))):
            raise EvidenceMalformedError("package omits request known_weaknesses")
        if list(projection.get("disabled_regimes", ())) != list(
            request_context["objectives"].get("disabled_regimes", ())
        ):
            raise EvidenceMalformedError("package disabled_regimes differ from request")
        # Prevent the v2 envelope's top-level policy summary from diverging from
        # the package-bound values. The request JSON uses canonical Decimal tags.
        request_risk = request_context.get("research_risk_fraction")
        if isinstance(request_risk, Mapping):
            request_risk_value = request_risk.get("$decimal")
            if risk.get("research_risk_fraction") != {"$decimal": request_risk_value}:
                # Factory serialization may retain Decimal in the projection while
                # the envelope uses a fixed decimal string; compare numerically below.
                try:
                    if Decimal(str(risk.get("research_risk_fraction"))) != Decimal(
                        str(request_risk_value)
                    ):
                        raise EvidenceMalformedError("package risk fraction differs from request")
                except InvalidOperation as exc:
                    raise EvidenceMalformedError("package risk fraction is malformed") from exc
        expected_risk = request_context.get("research_risk_fraction")
        expected_drawdown = request_context.get("maximum_drawdown_fraction")
        for key, expected in (
            ("maximum_risk_fraction", expected_risk),
            ("maximum_drawdown_fraction", expected_drawdown),
        ):
            raw_expected = expected.get("$decimal") if isinstance(expected, Mapping) else expected
            try:
                if Decimal(str(envelope_risk.get(key))) != Decimal(str(raw_expected)):
                    raise EvidenceMalformedError("audit risk summary differs from package request")
            except InvalidOperation as exc:
                raise EvidenceMalformedError("audit risk summary is malformed") from exc
        projected_risk = risk.get("research_risk_fraction")
        if isinstance(projected_risk, Mapping):
            projected_risk = projected_risk.get("$decimal")
        try:
            if Decimal(str(projected_risk)) != Decimal(
                str(
                    request_risk.get("$decimal")
                    if isinstance(request_risk, Mapping)
                    else request_risk
                )
            ):
                raise EvidenceMalformedError("package research risk differs from request")
        except InvalidOperation as exc:
            raise EvidenceMalformedError("package research risk is malformed") from exc
        constraint_drawdown = constraints.get("maximum_drawdown_fraction")
        try:
            if isinstance(constraint_drawdown, Mapping):
                constraint_drawdown = constraint_drawdown.get("$decimal")
            if Decimal(str(constraint_drawdown)) != Decimal(
                str(
                    expected_drawdown.get("$decimal")
                    if isinstance(expected_drawdown, Mapping)
                    else expected_drawdown
                )
            ):
                raise EvidenceMalformedError("package drawdown constraint differs from request")
        except InvalidOperation as exc:
            raise EvidenceMalformedError("package drawdown constraint is malformed") from exc

    def _validate_lineage(self, lineage: Mapping[str, Any]) -> None:
        for key in (
            "dataset_id",
            "registry_version",
            "instrument",
            "exchange",
            "market_type",
        ):
            _identifier(lineage.get(key), f"dataset_lineage.{key}")
        for key in ("manifest_sha256", "dataset_sha256"):
            _sha(lineage.get(key), f"dataset_lineage.{key}")
        timeframes = _sequence(lineage.get("timeframes"), "dataset_lineage.timeframes")
        if not timeframes or len(timeframes) != len(set(timeframes)):
            raise EvidenceMalformedError("dataset timeframes must be non-empty and unique")
        if lineage.get("immutable") is not True or lineage.get("tamper_verified") is not True:
            raise EvidenceMalformedError("dataset lineage must be immutable and tamper-verified")
        market_type = lineage["market_type"]
        if self._policy.require_funding_lineage_for_futures and market_type in {
            "linear_futures",
            "inverse_futures",
            "perpetual",
        }:
            _sha(lineage.get("funding_lineage_sha256"), "funding_lineage_sha256")

    def _validate_research(
        self,
        research: Mapping[str, Any],
        candidate_id: str,
        configuration_hash: str,
    ) -> None:
        if _identifier(research.get("candidate_id"), "research.candidate_id") != candidate_id:
            raise EvidenceMalformedError("research evidence belongs to another candidate")
        if (
            _sha(research.get("configuration_hash"), "research.configuration_hash")
            != configuration_hash
        ):
            raise EvidenceMalformedError("research configuration hash mismatch")
        periods = _mapping(research.get("periods"), "research.periods")
        expected = ("train", "validation", "test", "holdout")
        if set(periods) != set(expected):
            raise EvidenceMalformedError(
                "research periods must contain train/validation/test/holdout"
            )
        ranges = [_time_range(periods[name], f"research.periods.{name}") for name in expected]
        if not all(left[1] <= right[0] for left, right in itertools.pairwise(ranges)):
            raise EvidenceMalformedError("research periods overlap or are out of order")
        if (
            research.get("holdout_isolated") is not True
            or research.get("holdout_opened_once") is not True
        ):
            raise EvidenceMalformedError("holdout isolation proof is missing")
        seeds = _sequence(research.get("random_seeds"), "research.random_seeds")
        if not seeds or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds):
            raise EvidenceMalformedError("research random_seeds must be non-empty integers")
        results = _mapping(research.get("results_evidence"), "research.results_evidence")
        packages = _sequence(results.get("registry_package_hashes"), "registry_package_hashes")
        if len(packages) < self._policy.minimum_results_packages:
            raise EvidenceMalformedError("insufficient Results Aggregator packages")
        for item in packages:
            _sha(item, "registry package hash")

    @staticmethod
    def _validate_execution(execution: Mapping[str, Any]) -> None:
        if execution.get("mode") != "paper":
            raise EvidenceMalformedError("execution mode must be paper")
        for key in ("live_trading_authorized", "private_exchange_access", "order_placement"):
            if execution.get(key) is not False:
                raise EvidenceMalformedError(f"execution assumption {key} must be false")
        _identifier(execution.get("exchange"), "execution.exchange")
        _identifier(execution.get("market_type"), "execution.market_type")

    def _validate_risk(self, risk: Mapping[str, Any]) -> bool:
        if risk.get("paper_only") is not True:
            raise EvidenceMalformedError("risk policy must be paper-only")
        risk_fraction = _decimal(risk.get("maximum_risk_fraction"), "maximum_risk_fraction")
        drawdown = _decimal(risk.get("maximum_drawdown_fraction"), "maximum_drawdown_fraction")
        if not Decimal("0") < risk_fraction <= Decimal("1"):
            raise EvidenceMalformedError("maximum_risk_fraction must be in (0, 1]")
        if not Decimal("0") < drawdown <= Decimal("1"):
            raise EvidenceMalformedError("maximum_drawdown_fraction must be in (0, 1]")
        return (
            risk_fraction <= self._policy.maximum_risk_fraction
            and drawdown <= self._policy.maximum_drawdown_fraction
        )


def _validate_v2_request_context(request: Mapping[str, Any]) -> datetime:
    """Validate the complete Factory v1 request representation used by intent v2.

    This deliberately mirrors the plain serialized contract without importing
    Strategy Factory. The fingerprint proves byte identity; these checks prove
    that the bytes describe a well-formed request under the supported schema.
    """

    _reject_secret_keys(request, "package intent request")
    if _strict_int(request.get("schema_version"), "request.schema_version") != 1:
        raise EvidenceMalformedError("unsupported Factory request schema in package intent")

    request_id = _identifier(request.get("request_id"), "request.request_id")
    del request_id
    mode = request.get("mode")
    modes = {"create", "adapt", "improve", "validate"}
    if not isinstance(mode, str) or mode not in modes:
        raise EvidenceMalformedError("request.mode is unsupported")
    symbol = _identifier(request.get("symbol"), "request.symbol")
    exchange = _identifier(request.get("exchange"), "request.exchange")
    market_type = request.get("market_type")
    market_types = {"spot", "linear_futures", "inverse_futures"}
    if not isinstance(market_type, str) or market_type not in market_types:
        raise EvidenceMalformedError("request.market_type is unsupported")

    dataset = _mapping(request.get("dataset"), "request.dataset")
    _exact_keys(
        dataset,
        {
            "dataset_id",
            "source",
            "symbol",
            "exchange",
            "market_type",
            "base_timeframe",
            "available_timeframes",
            "period",
            "timezone",
            "file_format",
            "uri",
            "sha256",
            "row_count",
            "received_at",
            "schema_version",
            "funding_uri",
            "funding_sha256",
            "exchange_info_sha256",
        },
        "request.dataset",
    )
    for name in ("dataset_id", "source", "symbol", "exchange", "base_timeframe"):
        _identifier(dataset.get(name), f"request.dataset.{name}")
    if (
        dataset.get("symbol") != symbol
        or dataset.get("exchange") != exchange
        or dataset.get("market_type") != market_type
    ):
        raise EvidenceMalformedError("request identity differs from dataset descriptor")
    available = _identifier_array(
        dataset.get("available_timeframes"), "request.dataset.available_timeframes"
    )
    if not available or dataset.get("base_timeframe") not in available:
        raise EvidenceMalformedError("dataset timeframes must include its base timeframe")
    dataset_period = _time_range(dataset.get("period"), "request.dataset.period")
    if dataset.get("timezone") != "UTC":
        raise EvidenceMalformedError("request.dataset.timezone must be UTC")
    if dataset.get("file_format") not in {"csv", "parquet"}:
        raise EvidenceMalformedError("request.dataset.file_format is unsupported")
    uri = dataset.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise EvidenceMalformedError("request.dataset.uri must be non-blank")
    _validate_dataset_uri(uri, field="request.dataset.uri")
    _sha(dataset.get("sha256"), "request.dataset.sha256")
    _strict_int(dataset.get("row_count"), "request.dataset.row_count", minimum=1)
    _canonical_datetime(dataset.get("received_at"), "request.dataset.received_at")
    if _strict_int(dataset.get("schema_version"), "request.dataset.schema_version") != 1:
        raise EvidenceMalformedError("request.dataset.schema_version is unsupported")
    funding_uri = dataset.get("funding_uri")
    funding_sha = dataset.get("funding_sha256")
    if funding_uri is not None and (not isinstance(funding_uri, str) or not funding_uri.strip()):
        raise EvidenceMalformedError("request.dataset.funding_uri must be non-blank or null")
    if isinstance(funding_uri, str):
        _validate_dataset_uri(funding_uri, field="request.dataset.funding_uri")
    if funding_uri is not None and funding_sha is None:
        raise EvidenceMalformedError("request.dataset.funding_uri requires funding_sha256")
    if funding_sha is not None:
        _sha(funding_sha, "request.dataset.funding_sha256")
    exchange_info_sha = dataset.get("exchange_info_sha256")
    if exchange_info_sha is not None:
        _sha(exchange_info_sha, "request.dataset.exchange_info_sha256")

    requested_timeframes = _string_array(
        request.get("allowed_timeframes"), "request.allowed_timeframes"
    )
    if not requested_timeframes or len(set(requested_timeframes)) != len(requested_timeframes):
        raise EvidenceMalformedError("request.allowed_timeframes must be non-empty and unique")
    if any(item not in available for item in requested_timeframes):
        raise EvidenceMalformedError("request.allowed_timeframes exceed dataset availability")

    initial_capital = _decimal_tag(request.get("initial_capital"), "request.initial_capital")
    if initial_capital < Decimal("0.00000001"):
        raise EvidenceMalformedError("request.initial_capital is below its minimum")
    for name in ("research_risk_fraction", "maximum_drawdown_fraction"):
        value = _decimal_tag(request.get(name), f"request.{name}")
        if value < 0 or value > 1:
            raise EvidenceMalformedError(f"request.{name} must be in [0, 1]")
    _strict_int(request.get("minimum_trade_count"), "request.minimum_trade_count", minimum=1)
    _strict_int(request.get("random_seed"), "request.random_seed", minimum=0)

    families = _identifier_array(request.get("allowed_strategy_families"), "request families")
    if not families or tuple(sorted(families)) != families:
        raise EvidenceMalformedError(
            "request.allowed_strategy_families must be non-empty and sorted"
        )
    forbidden = _identifier_array(request.get("forbidden_module_ids"), "request forbidden modules")
    if tuple(sorted(forbidden)) != forbidden:
        raise EvidenceMalformedError("request.forbidden_module_ids must be sorted")
    for name in ("baseline_strategy_id", "source_strategy_id"):
        identity = request.get(name)
        if identity is not None:
            _identifier(identity, f"request.{name}")
    if mode == "adapt" and request.get("source_strategy_id") is None:
        raise EvidenceMalformedError("adapt request requires source_strategy_id")
    if mode in {"improve", "validate"} and request.get("baseline_strategy_id") is None:
        raise EvidenceMalformedError(f"{mode} request requires baseline_strategy_id")

    budget = _mapping(request.get("budget"), "request.budget")
    _exact_keys(
        budget,
        {
            "maximum_experiments",
            "maximum_architectures",
            "maximum_finalists",
            "maximum_duration_seconds",
            "maximum_memory_bytes",
            "maximum_cpu_workers",
            "maximum_parallel_tasks",
            "stage_experiment_limits",
            "schema_version",
        },
        "request.budget",
    )
    budget_limits: dict[str, int] = {}
    for name in (
        "maximum_experiments",
        "maximum_architectures",
        "maximum_finalists",
        "maximum_duration_seconds",
        "maximum_memory_bytes",
        "maximum_cpu_workers",
        "maximum_parallel_tasks",
    ):
        budget_limits[name] = _strict_int(budget.get(name), f"request.budget.{name}", minimum=1)
    if _strict_int(budget.get("schema_version"), "request.budget.schema_version") != 1:
        raise EvidenceMalformedError("request.budget.schema_version is unsupported")
    stage_limits = _mapping(budget.get("stage_experiment_limits"), "request budget stage limits")
    known_stages = {
        "data_gate",
        "instrument_profile",
        "regime_discovery",
        "hypothesis_generation",
        "architecture_generation",
        "experiment_planning",
        "structural_screening",
        "coarse_search",
        "local_refinement",
        "walk_forward",
        "robustness",
        "holdout",
        "simplicity_selection",
        "final_audit",
        "package",
        "paper_admission",
    }
    if not stage_limits or set(stage_limits) - known_stages:
        raise EvidenceMalformedError("request budget stage limits are empty or unsupported")
    stage_total = 0
    for name, limit in stage_limits.items():
        stage_total += _strict_int(
            limit, f"request.budget.stage_experiment_limits.{name}", minimum=1
        )
    if stage_total > budget_limits["maximum_experiments"]:
        raise EvidenceMalformedError("request budget stage limits exceed maximum_experiments")

    adaptation = _mapping(request.get("adaptation_policy"), "request.adaptation_policy")
    _exact_keys(
        adaptation,
        {
            "allow_parameter_adaptation",
            "allowed_parameter_names",
            "neighborhood_points",
            "require_source_baseline_control",
            "allow_direction_reduction",
            "schema_version",
        },
        "request.adaptation_policy",
    )
    for name in (
        "allow_parameter_adaptation",
        "require_source_baseline_control",
        "allow_direction_reduction",
    ):
        _strict_bool(adaptation.get(name), f"request.adaptation_policy.{name}")
    adaptation_names = _qualified_parameter_array(
        adaptation.get("allowed_parameter_names"),
        "request.adaptation_policy.allowed_parameter_names",
    )
    if tuple(sorted(adaptation_names)) != adaptation_names:
        raise EvidenceMalformedError("adaptation parameter names must be sorted")
    _strict_int(
        adaptation.get("neighborhood_points"),
        "request.adaptation_policy.neighborhood_points",
        minimum=1,
        maximum=21,
    )
    if (
        _strict_int(adaptation.get("schema_version"), "request.adaptation_policy.schema_version")
        != 1
    ):
        raise EvidenceMalformedError("request.adaptation_policy.schema_version is unsupported")

    improvement = _mapping(request.get("improvement_policy"), "request.improvement_policy")
    _exact_keys(
        improvement,
        {
            "allowed_mutations",
            "allowed_parameter_names",
            "neighborhood_points",
            "maximum_runtime_module_changes",
            "maximum_architectures",
            "maximum_complexity_increase",
            "require_baseline_control",
            "schema_version",
        },
        "request.improvement_policy",
    )
    allowed_mutations = _string_array(
        improvement.get("allowed_mutations"), "request.improvement_policy.allowed_mutations"
    )
    valid_mutations = {
        "parameters",
        "context_filters",
        "entry",
        "protection",
        "target",
        "position_management",
        "portfolio_constraints",
        "directions",
    }
    if (
        not allowed_mutations
        or len(set(allowed_mutations)) != len(allowed_mutations)
        or set(allowed_mutations) - valid_mutations
        or tuple(sorted(allowed_mutations)) != allowed_mutations
    ):
        raise EvidenceMalformedError("request.improvement_policy.allowed_mutations is invalid")
    improvement_names = _qualified_parameter_array(
        improvement.get("allowed_parameter_names"),
        "request.improvement_policy.allowed_parameter_names",
    )
    if tuple(sorted(improvement_names)) != improvement_names:
        raise EvidenceMalformedError("improvement parameter names must be sorted")
    _strict_int(
        improvement.get("neighborhood_points"),
        "request.improvement_policy.neighborhood_points",
        minimum=1,
        maximum=21,
    )
    module_changes = _strict_int(
        improvement.get("maximum_runtime_module_changes"),
        "request.improvement_policy.maximum_runtime_module_changes",
        minimum=0,
    )
    _strict_int(
        improvement.get("maximum_architectures"),
        "request.improvement_policy.maximum_architectures",
        minimum=1,
    )
    if (
        _decimal_tag(
            improvement.get("maximum_complexity_increase"),
            "request.improvement_policy.maximum_complexity_increase",
        )
        < 0
    ):
        raise EvidenceMalformedError("improvement complexity increase must be non-negative")
    _strict_bool(
        improvement.get("require_baseline_control"),
        "request.improvement_policy.require_baseline_control",
    )
    if (
        _strict_int(improvement.get("schema_version"), "request.improvement_policy.schema_version")
        != 1
    ):
        raise EvidenceMalformedError("request.improvement_policy.schema_version is unsupported")
    if set(allowed_mutations) - {"parameters"} and module_changes < 1:
        raise EvidenceMalformedError("structural improvements require module changes")

    explicit_holdout = request.get("explicit_holdout")
    if explicit_holdout is not None:
        holdout = _time_range(explicit_holdout, "request.explicit_holdout")
        if holdout[0] < dataset_period[0] or holdout[1] > dataset_period[1]:
            raise EvidenceMalformedError("request explicit_holdout exceeds dataset period")
    if mode == "validate" and explicit_holdout is None:
        raise EvidenceMalformedError("validate request requires explicit_holdout")
    request_created = _canonical_datetime(request.get("created_at"), "request.created_at")
    _validate_v2_objectives(request.get("objectives"), request_context=request)
    return request_created


def _validate_v2_objectives(raw: object, *, request_context: Mapping[str, Any]) -> None:
    """Independently enforce the versioned typed Factory objective projection."""

    objectives = _mapping(raw, "package intent objectives")
    for key, value in objectives.items():
        if key == "strategy_version":
            if not isinstance(value, str) or not value.strip():
                raise EvidenceMalformedError("package intent strategy_version is malformed")
        elif key in {"known_weaknesses", "disabled_regimes"}:
            values = _sequence(value, f"package intent {key}")
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise EvidenceMalformedError(f"package intent {key} is malformed")
        elif key in {"risk_policy", "execution_assumptions"}:
            allowed = (
                {"research_risk_fraction", "initial_capital"}
                if key == "risk_policy"
                else {"exchange", "market_type"}
            )
            _validate_scalar_mapping(value, f"package intent {key}", allowed=allowed)
            expected = (
                {
                    "research_risk_fraction": request_context.get("research_risk_fraction"),
                    "initial_capital": request_context.get("initial_capital"),
                }
                if key == "risk_policy"
                else {
                    "exchange": request_context.get("exchange"),
                    "market_type": request_context.get("market_type"),
                }
            )
            if any(value[field] != expected[field] for field in value):
                raise EvidenceMalformedError(f"package intent {key} conflicts with request")
        elif key == "cost_scenarios":
            scenarios = _sequence(value, f"package intent {key}")
            for index, scenario in enumerate(scenarios):
                _validate_cost_scenario(scenario, f"package intent {key}[{index}]")
        elif key == "robustness_scenarios":
            if not isinstance(value, list) or value:
                raise EvidenceMalformedError(
                    "package intent robustness scenarios lack a typed owner contract"
                )
        elif key == "walk_forward":
            if value is None:
                continue
            walk = _mapping(value, "package intent walk_forward")
            allowed = {
                "mode",
                "train_seconds",
                "validation_seconds",
                "oos_seconds",
                "step_seconds",
                "purge_seconds",
                "embargo_seconds",
                "minimum_history_seconds",
            }
            if set(walk) - allowed:
                raise EvidenceMalformedError("package intent walk_forward fields differ from v2")
            if "mode" in walk and walk["mode"] not in {"rolling", "anchored"}:
                raise EvidenceMalformedError("package intent walk_forward mode is unsupported")
            for name, item in walk.items():
                if name == "mode":
                    continue
                minimum = (
                    1
                    if name
                    in {
                        "train_seconds",
                        "validation_seconds",
                        "oos_seconds",
                        "step_seconds",
                    }
                    else 0
                )
                if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
                    raise EvidenceMalformedError(f"package intent walk_forward {name} is malformed")
        else:
            if (
                not _OBJECTIVE_METRIC.fullmatch(key)
                or not isinstance(value, str)
                or value not in {"maximize", "minimize"}
            ):
                raise EvidenceMalformedError("package intent ranking objective is malformed")


def _validate_scalar_mapping(raw: object, name: str, *, allowed: set[str]) -> None:
    mapping = _mapping(raw, name)
    for key, value in mapping.items():
        if not isinstance(key, str) or not key.strip():
            raise EvidenceMalformedError(f"{name} keys must be non-blank strings")
        if key not in allowed:
            raise EvidenceMalformedError(f"unsupported decision-relevant field {name}.{key}")
        if value is None or isinstance(value, (str, bool, int, float)):
            continue
        if isinstance(value, Mapping) and set(value) == {"$decimal"}:
            if not isinstance(value["$decimal"], str) or not value["$decimal"]:
                raise EvidenceMalformedError(f"{name}.{key} decimal is malformed")
            continue
        raise EvidenceMalformedError(f"{name}.{key} must be a JSON scalar")


def _strict_int(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceMalformedError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise EvidenceMalformedError(f"{name} is below its minimum")
    if maximum is not None and value > maximum:
        raise EvidenceMalformedError(f"{name} exceeds its maximum")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceMalformedError(f"{name} must be boolean")
    return value


def _decimal_tag(value: object, name: str) -> Decimal:
    mapping = _mapping(value, name)
    if set(mapping) != {"$decimal"} or not isinstance(mapping["$decimal"], str):
        raise EvidenceMalformedError(f"{name} must use the canonical Decimal tag")
    raw = mapping["$decimal"]
    result = _decimal(raw, name)
    if format(result, "f") != raw:
        raise EvidenceMalformedError(f"{name} is not a canonical Decimal string")
    return result


def _canonical_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceMalformedError(f"{name} must be a canonical UTC datetime string")
    parsed = _datetime(value, name)
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise EvidenceMalformedError(f"{name} is not in canonical UTC form")
    return parsed


def _string_array(value: object, name: str) -> tuple[str, ...]:
    values = _sequence(value, name)
    if any(not isinstance(item, str) for item in values):
        raise EvidenceMalformedError(f"{name} must contain only strings")
    return values


def _identifier_array(value: object, name: str) -> tuple[str, ...]:
    values = _string_array(value, name)
    for item in values:
        _identifier(item, name)
    if len(set(values)) != len(values):
        raise EvidenceMalformedError(f"{name} must contain unique identifiers")
    return values


def _qualified_parameter_array(value: object, name: str) -> tuple[str, ...]:
    values = _identifier_array(value, name)
    if any("." not in item for item in values):
        raise EvidenceMalformedError(f"{name} must contain runtime-qualified names")
    return values


def _reject_secret_keys(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_") if isinstance(key, str) else ""
            if _contains_secret_token(normalized):
                raise EvidenceMalformedError(f"secret-like field is forbidden in {path}")
            _reject_secret_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_keys(item, f"{path}[{index}]")


def _validate_dataset_uri(uri: str, *, field: str) -> None:
    """Reject credential-bearing URI forms without echoing their contents."""

    try:
        parsed = urlsplit(uri)
        if parsed.username is not None or parsed.password is not None:
            raise EvidenceMalformedError(f"{field} must not contain credentials")
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = name.lower().replace("-", "_")
            if _contains_secret_token(normalized):
                raise EvidenceMalformedError(
                    f"{field} must not contain credential query parameters"
                )
    except ValueError as exc:
        raise EvidenceMalformedError(f"{field} is malformed") from exc


def _contains_secret_token(normalized_name: str) -> bool:
    return any(
        token in normalized_name
        for token in (
            "secret",
            "api_key",
            "apikey",
            "private_key",
            "access_token",
            "refresh_token",
            "password",
        )
    )


def _validate_cost_scenario(raw: object, name: str) -> None:
    scenario = _mapping(raw, name)
    if set(scenario) != {"scenario_id", "roles", "execution_cost_multiplier"}:
        raise EvidenceMalformedError(f"{name} fields differ from supported cost-double contract")
    if scenario.get("scenario_id") != "cost-double-v1":
        raise EvidenceMalformedError(f"{name}.scenario_id is unsupported")
    if scenario.get("roles") != ["oos"]:
        raise EvidenceMalformedError(f"{name}.roles differs from supported OOS role")
    if scenario.get("execution_cost_multiplier") != 2:
        raise EvidenceMalformedError(f"{name}.execution_cost_multiplier is unsupported")


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceMalformedError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise EvidenceMalformedError(f"{name} keys must be strings")
    return value


def _sequence(value: object, name: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise EvidenceMalformedError(f"{name} must be an array")
    return tuple(value)


def _exact_keys(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    missing = keys - set(value)
    unknown = set(value) - keys
    if missing or unknown:
        raise EvidenceMalformedError(
            f"{name} fields differ from schema; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value):
        raise EvidenceMalformedError(f"{name} must be a portable identifier")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase SHA-256")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise EvidenceMalformedError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise EvidenceMalformedError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise EvidenceMalformedError(f"{name} must be finite")
    return result


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if not isinstance(value, str):
        raise EvidenceMalformedError(f"{name} must be an ISO-8601 datetime")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise EvidenceMalformedError(f"{name} must be an ISO-8601 datetime") from exc
    return _utc(parsed, name)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceMalformedError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _time_range(value: object, name: str) -> tuple[datetime, datetime]:
    mapping = _mapping(value, name)
    if set(mapping) != {"start", "end"}:
        raise EvidenceMalformedError(f"{name} must contain start and end")
    start = _datetime(mapping["start"], f"{name}.start")
    end = _datetime(mapping["end"], f"{name}.end")
    if end <= start:
        raise EvidenceMalformedError(f"{name}.end must be later than start")
    return start, end
