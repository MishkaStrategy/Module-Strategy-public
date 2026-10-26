from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from pydantic import ValidationError

from .acceptance import (
    AcceptanceCurrent,
    AcceptanceMutationResult,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
)
from .api_contracts import ApiError
from .backtest import BacktestJobRecord, BacktestResultSummary
from .contracts import ComponentHealth, ComponentId, SystemOverview
from .evidence import (
    AuditDecisionList,
    AuditEvidenceList,
    EvidenceOverview,
    ResultsEvidenceList,
)
from .market_data import MarketDatasetList, MarketDatasetSummary, MarketDatasetValidation
from .paper import PaperBlockedScopeList, PaperOrderList, PaperRuntimeSummary
from .research import (
    ResearchExperimentList,
    ResearchExperimentSummary,
    ResearchRunList,
    ResearchRunSummary,
)
from .strategy import StrategyRunList, StrategyRunSummary
from .system_resources import SystemResourcesSnapshot

_MAX_RESPONSE_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]
MutationTransport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


@dataclass(frozen=True)
class ClientSettings:
    base_url: str = "http://127.0.0.1:8765"
    reader_token: str | None = field(default=None, repr=False)
    operator_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", validate_control_base_url(self.base_url))
        for name, value in (
            ("reader_token", self.reader_token),
            ("operator_token", self.operator_token),
        ):
            if value is not None and (not value or len(value) > 4096):
                raise ValueError(f"{name} must contain 1..4096 characters when configured")
        if not 0.05 <= self.timeout_seconds <= 30.0:
            raise ValueError("timeout_seconds must be between 0.05 and 30")


class ControlClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        action: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action
        self.status_code = status_code


class ControlClient:
    """Small monitoring/control client that imports no component engine or HTTP framework."""

    def __init__(
        self,
        settings: ClientSettings | None = None,
        *,
        transport: Transport | None = None,
        mutation_transport: MutationTransport | None = None,
    ) -> None:
        self._settings = settings or ClientSettings()
        self._transport = _default_transport if transport is None else transport
        self._mutation_transport = (
            _default_mutation_transport if mutation_transport is None else mutation_transport
        )

    def status(self) -> SystemOverview:
        payload = self._get("/api/control/v1/health")
        try:
            return SystemOverview.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("system health") from exc

    def component_health(self, component: ComponentId) -> ComponentHealth:
        if not isinstance(component, ComponentId):
            raise TypeError("component must be ComponentId")
        payload = self._get(f"/api/control/v1/components/{component.value}/health")
        try:
            return ComponentHealth.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("component health") from exc

    def system_resources(self) -> SystemResourcesSnapshot:
        payload = self._get("/api/control/v1/system/resources")
        try:
            return SystemResourcesSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("System Resources") from exc

    def datasets(self, *, limit: int = 50, offset: int = 0) -> MarketDatasetList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/market-data/datasets?{query}")
        try:
            return MarketDatasetList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Market Data dataset list") from exc

    def dataset(self, dataset_id: str, *, version: int | None = None) -> MarketDatasetSummary:
        segment = _path_segment(dataset_id, "dataset_id", maximum_length=128)
        query = _optional_version_query(version)
        payload = self._get(f"/api/control/v1/market-data/datasets/{segment}{query}")
        try:
            return MarketDatasetSummary.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Market Data dataset") from exc

    def dataset_validation(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        deep: bool = False,
    ) -> MarketDatasetValidation:
        segment = _path_segment(dataset_id, "dataset_id", maximum_length=128)
        if not isinstance(deep, bool):
            raise TypeError("deep must be bool")
        parameters: dict[str, str | int] = {"deep": "true" if deep else "false"}
        if version is not None:
            _positive_version(version)
            parameters["version"] = version
        query = urlencode(parameters)
        payload = self._get(f"/api/control/v1/market-data/datasets/{segment}/validation?{query}")
        try:
            return MarketDatasetValidation.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Market Data dataset validation") from exc

    def strategy_runs(self, *, limit: int = 50, offset: int = 0) -> StrategyRunList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/strategy/runs?{query}")
        try:
            return StrategyRunList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Strategy Factory run list") from exc

    def strategy_run(self, run_id: str) -> StrategyRunSummary:
        segment = _path_segment(run_id, "run_id")
        payload = self._get(f"/api/control/v1/strategy/runs/{segment}")
        try:
            return StrategyRunSummary.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Strategy Factory run") from exc

    def backtests(self, *, limit: int = 50, offset: int = 0) -> tuple[BacktestJobRecord, ...]:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/backtests?{query}")
        if not isinstance(payload, list):
            raise _invalid_response("Backtest job list")
        try:
            return tuple(BacktestJobRecord.model_validate(item) for item in payload)
        except ValidationError as exc:
            raise _invalid_response("Backtest job list") from exc

    def backtest(self, job_id: str) -> BacktestJobRecord:
        segment = _path_segment(job_id, "job_id")
        payload = self._get(f"/api/control/v1/backtests/{segment}")
        try:
            return BacktestJobRecord.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Backtest job") from exc

    def backtest_result(self, job_id: str) -> BacktestResultSummary:
        segment = _path_segment(job_id, "job_id")
        payload = self._get(f"/api/control/v1/backtests/{segment}/result")
        try:
            return BacktestResultSummary.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Backtest result") from exc

    def research_experiments(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> ResearchExperimentList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/research/experiments?{query}")
        try:
            return ResearchExperimentList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Research experiment list") from exc

    def research_experiment(self, experiment_id: str) -> ResearchExperimentSummary:
        segment = _path_segment(experiment_id, "experiment_id")
        payload = self._get(f"/api/control/v1/research/experiments/{segment}")
        try:
            return ResearchExperimentSummary.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Research experiment") from exc

    def research_runs(
        self,
        experiment_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ResearchRunList:
        segment = _path_segment(experiment_id, "experiment_id")
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/research/experiments/{segment}/runs?{query}")
        try:
            return ResearchRunList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Research run list") from exc

    def research_run(self, run_id: str) -> ResearchRunSummary:
        segment = _path_segment(run_id, "run_id")
        payload = self._get(f"/api/control/v1/research/runs/{segment}")
        try:
            return ResearchRunSummary.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Research run") from exc

    def paper_runtime(self) -> PaperRuntimeSummary:
        payload = self._get("/api/control/v1/paper/runtime")
        try:
            return PaperRuntimeSummary.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Paper Runtime summary") from exc

    def paper_orders(self, *, limit: int = 50, offset: int = 0) -> PaperOrderList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/paper/orders?{query}")
        try:
            return PaperOrderList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Paper order list") from exc

    def paper_blocked_scopes(self) -> PaperBlockedScopeList:
        payload = self._get("/api/control/v1/paper/blocked-scopes")
        try:
            return PaperBlockedScopeList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Paper blocked-scope list") from exc

    def acceptance_current(self) -> AcceptanceCurrent:
        payload = self._get("/api/control/v1/acceptance/current")
        try:
            return AcceptanceCurrent.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("System Acceptance current run") from exc

    def acceptance_start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult:
        return self._acceptance_mutation("/api/control/v1/acceptance/start", request)

    def acceptance_stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._acceptance_mutation("/api/control/v1/acceptance/stop", request)

    def acceptance_cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._acceptance_mutation("/api/control/v1/acceptance/cancel", request)

    def acceptance_retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._acceptance_mutation("/api/control/v1/acceptance/retry", request)

    def _acceptance_mutation(
        self,
        path: str,
        request: AcceptanceStartRequest | AcceptanceRunMutationRequest,
    ) -> AcceptanceMutationResult:
        payload = self._post(path, request.model_dump(mode="json"))
        try:
            return AcceptanceMutationResult.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("System Acceptance mutation") from exc

    def evidence_overview(self) -> EvidenceOverview:
        payload = self._get("/api/control/v1/evidence/overview")
        try:
            return EvidenceOverview.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Evidence overview") from exc

    def audit_decisions(self, *, limit: int = 50, offset: int = 0) -> AuditDecisionList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/evidence/audit/decisions?{query}")
        try:
            return AuditDecisionList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Strategy Audit decision list") from exc

    def audit_evidence(self, *, limit: int = 50, offset: int = 0) -> AuditEvidenceList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/evidence/audit/evidence?{query}")
        try:
            return AuditEvidenceList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Strategy Audit evidence list") from exc

    def results_evidence(self, *, limit: int = 50, offset: int = 0) -> ResultsEvidenceList:
        query = _pagination_query(limit=limit, offset=offset)
        payload = self._get(f"/api/control/v1/evidence/results?{query}")
        try:
            return ResultsEvidenceList.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_response("Results evidence list") from exc

    def _get(self, path: str) -> object:
        headers = {"Accept": "application/json"}
        if self._settings.reader_token is not None:
            headers["Authorization"] = f"Bearer {self._settings.reader_token}"
        url = f"{self._settings.base_url}{path}"
        try:
            status_code, body = self._transport(url, headers, self._settings.timeout_seconds)
        except ControlClientError:
            raise
        except Exception as exc:
            raise ControlClientError(
                "CONTROL_TRANSPORT_FAILED",
                "Could not connect to the local Control Plane.",
                action="Start modular-control-api and verify its loopback host and port.",
            ) from exc
        return _decode_response(status_code, body)

    def _post(self, path: str, payload: dict[str, object]) -> object:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._settings.operator_token is not None:
            headers["Authorization"] = f"Bearer {self._settings.operator_token}"
        url = f"{self._settings.base_url}{path}"
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            status_code, response = self._mutation_transport(
                url,
                headers,
                body,
                self._settings.timeout_seconds,
            )
        except ControlClientError:
            raise
        except Exception as exc:
            raise ControlClientError(
                "CONTROL_TRANSPORT_FAILED",
                "Could not connect to the local Control Plane.",
                action="Start modular-control-api and verify its loopback host and port.",
            ) from exc
        return _decode_response(status_code, response)

    def __repr__(self) -> str:
        return (
            "ControlClient("
            f"base_url={self._settings.base_url!r}, "
            f"reader_token_configured={self._settings.reader_token is not None}, "
            f"operator_token_configured={self._settings.operator_token is not None})"
        )


def validate_control_base_url(base_url: str) -> str:
    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "http":
        raise ValueError("Control Plane URL must use plain HTTP on the local loopback boundary")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("Control Plane URL must target a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Control Plane URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Control Plane URL must not contain query or fragment data")
    if parsed.path not in {"", "/"}:
        raise ValueError("Control Plane URL must not contain a path prefix")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Control Plane URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Control Plane URL port must be between 1 and 65535")
    normalized_host = parsed.hostname.lower()
    host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return f"http://{host}{f':{port}' if port is not None else ''}"


def _pagination_query(*, limit: int, offset: int) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    return urlencode({"limit": limit, "offset": offset})


def _optional_version_query(version: int | None) -> str:
    if version is None:
        return ""
    _positive_version(version)
    return f"?{urlencode({'version': version})}"


def _positive_version(version: int) -> None:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")


def _path_segment(value: str, name: str, *, maximum_length: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > maximum_length:
        raise ValueError(
            f"{name} must contain 1..{maximum_length} characters without surrounding whitespace"
        )
    return quote(normalized, safe="")


def _invalid_response(surface: str) -> ControlClientError:
    return ControlClientError(
        "CONTROL_RESPONSE_INVALID",
        f"Control Plane returned an incompatible {surface} response.",
        action="Verify that modular-control and modular-control-api use compatible versions.",
    )


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    return _open_request(request, timeout)


def _default_mutation_transport(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, headers=headers, data=body, method="POST")
    return _open_request(request, timeout)


def _open_request(request: Request, timeout: float) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), _read_bounded(response.read)
    except HTTPError as exc:
        return exc.code, _read_bounded(exc.read)
    except URLError as exc:
        raise ControlClientError(
            "CONTROL_TRANSPORT_FAILED",
            "Could not connect to the local Control Plane.",
            action="Start modular-control-api and verify its loopback host and port.",
        ) from exc


def _decode_response(status_code: int, body: bytes) -> object:
    if status_code != 200:
        raise _remote_error(status_code, body)
    try:
        decoded: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ControlClientError(
            "CONTROL_RESPONSE_INVALID",
            "Control Plane returned invalid JSON.",
            action="Check Control Plane logs and version compatibility.",
        ) from exc
    return decoded


def _read_bounded(read: Callable[[int], bytes]) -> bytes:
    payload = read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ControlClientError(
            "CONTROL_RESPONSE_TOO_LARGE",
            "Control Plane response exceeded the monitoring payload limit.",
            action="Inspect Control Plane logs for an unexpected oversized response.",
        )
    return payload


def _remote_error(status_code: int, body: bytes) -> ControlClientError:
    try:
        error = ApiError.model_validate_json(body).error
    except (ValidationError, ValueError):
        return ControlClientError(
            "CONTROL_REQUEST_FAILED",
            f"Control Plane request failed with HTTP {status_code}.",
            action="Check Control Plane health and logs.",
            status_code=status_code,
        )
    return ControlClientError(
        error.code,
        error.message,
        action=error.action,
        status_code=status_code,
    )
