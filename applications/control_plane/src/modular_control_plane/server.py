from __future__ import annotations

import argparse
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

from .acceptance import AcceptanceControlService
from .acceptance_api import create_acceptance_router
from .adapters.backtest import ManagedBacktestRunner
from .adapters.evidence import ResultsRegistryEvidenceReader, StrategyAuditEvidenceReader
from .adapters.market_data import MarketDataHealthAdapter
from .adapters.market_data_http import (
    MarketDataHttpCatalogReader,
    MarketDataHttpHealthReader,
    validate_market_data_base_url,
)
from .adapters.research import ResearchRunnerStateReader
from .adapters.signal_bot_control_http import SignalBotControlHttpAdapter
from .adapters.signal_bot_http import (
    SignalBotHealthAdapter,
    SignalBotHttpHealthReader,
    validate_signal_bot_base_url,
)
from .adapters.strategy import StrategyFactoryStateReader
from .adapters.system_acceptance_http import (
    SystemAcceptanceHttpClient,
    validate_system_acceptance_base_url,
)
from .adapters.system_resources_psutil import PsutilSystemResourcesProvider
from .adapters.trading_runner_events_http import (
    TradingRunnerOperationalEventsHttpAdapter,
    validate_trading_runner_events_base_url,
)
from .adapters.trading_runner_lifecycle_http import (
    TradingRunnerLifecycleHttpAdapter,
    validate_trading_runner_lifecycle_base_url,
)
from .api import (
    OperatorAuthorizer,
    ReaderAuthorizer,
    create_api,
    operator_auth_required,
    reader_auth_required,
)
from .backtest import BacktestControlService, SQLiteBacktestJobStore
from .backtest_executor import BacktestJobExecutor
from .contracts import ComponentId
from .evidence import EvidenceControlService
from .evidence_api import create_evidence_router
from .market_data import MarketDataCatalogControlService
from .market_data_api import create_market_data_catalog_router
from .notifications import NotificationsControlService
from .notifications_api import create_notifications_router
from .observability_service import (
    ObservabilityBackendUnavailable,
    ObservabilityControlService,
    build_observability_control,
)
from .operational_events import OperationalEventsControlService
from .operational_events_api import create_operational_events_router
from .paper import PaperRuntimeControlService
from .paper_api import PaperPositionsReader, create_paper_router
from .paper_lifecycle import PaperLifecycleControlService
from .paper_lifecycle_api import create_paper_lifecycle_router
from .recovery_profile_api import create_recovery_profile_router
from .recovery_profiles import RecoveryProfileService
from .research import ResearchControlService
from .research_api import create_research_router
from .service import ComponentAdapter, ControlPlaneService
from .strategy import StrategyControlService
from .system_api import create_system_resources_router
from .system_resources import SystemResourcesControlService

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug"})
_DEFAULT_STATE_ROOT = Path("~/.module-strategy/control-plane").expanduser()


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    market_data_url: str = "http://127.0.0.1:8000"
    market_data_reader_token: str | None = field(default=None, repr=False)
    signal_bot_url: str | None = None
    signal_bot_control_url: str | None = None
    signal_bot_control_reader_token: str | None = field(default=None, repr=False)
    signal_bot_control_operator_token: str | None = field(default=None, repr=False)
    trading_runner_events_url: str | None = None
    trading_runner_events_reader_token: str | None = field(default=None, repr=False)
    trading_runner_lifecycle_url: str | None = None
    trading_runner_lifecycle_reader_token: str | None = field(default=None, repr=False)
    trading_runner_lifecycle_operator_token: str | None = field(default=None, repr=False)
    system_acceptance_url: str | None = None
    system_acceptance_reader_token: str | None = field(default=None, repr=False)
    system_acceptance_operator_token: str | None = field(default=None, repr=False)
    reader_token: str | None = field(default=None, repr=False)
    operator_token: str | None = field(default=None, repr=False)
    backtest_config_root: Path | None = None
    backtest_output_root: Path = _DEFAULT_STATE_ROOT / "backtest-results"
    backtest_state_path: Path = _DEFAULT_STATE_ROOT / "backtests.sqlite3"
    backtest_worker_count: int = 1
    strategy_factory_state_root: Path | None = None
    research_state_path: Path | None = None
    strategy_audit_state_path: Path | None = None
    results_registry_path: Path | None = None
    request_timeout_seconds: float = 2.0
    log_level: str = "info"

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", validate_bind_host(self.host))
        object.__setattr__(self, "port", validate_port(self.port))
        object.__setattr__(
            self,
            "market_data_url",
            validate_market_data_base_url(self.market_data_url),
        )
        if self.signal_bot_url is not None:
            object.__setattr__(
                self,
                "signal_bot_url",
                validate_signal_bot_base_url(self.signal_bot_url),
            )
        if self.signal_bot_control_url is not None:
            object.__setattr__(
                self,
                "signal_bot_control_url",
                validate_signal_bot_base_url(self.signal_bot_control_url),
            )
        if self.trading_runner_events_url is not None:
            object.__setattr__(
                self,
                "trading_runner_events_url",
                validate_trading_runner_events_base_url(self.trading_runner_events_url),
            )
        if self.trading_runner_lifecycle_url is not None:
            object.__setattr__(
                self,
                "trading_runner_lifecycle_url",
                validate_trading_runner_lifecycle_base_url(self.trading_runner_lifecycle_url),
            )
        if self.system_acceptance_url is not None:
            object.__setattr__(
                self,
                "system_acceptance_url",
                validate_system_acceptance_base_url(self.system_acceptance_url),
            )
        _validate_token(self.market_data_reader_token, "market_data_reader_token")
        _validate_token(self.reader_token, "reader_token")
        _validate_token(self.operator_token, "operator_token")
        _validate_token(
            self.signal_bot_control_reader_token,
            "signal_bot_control_reader_token",
        )
        _validate_token(
            self.signal_bot_control_operator_token,
            "signal_bot_control_operator_token",
        )
        if self.signal_bot_control_url is None and (
            self.signal_bot_control_reader_token is not None
            or self.signal_bot_control_operator_token is not None
        ):
            raise ValueError("Signal Bot downstream control tokens require signal_bot_control_url")
        if self.signal_bot_control_url is not None and (
            self.signal_bot_control_reader_token is None
            or self.signal_bot_control_operator_token is None
        ):
            raise ValueError(
                "Signal Bot control reader/operator tokens are required when owner control is enabled"
            )
        if (
            self.signal_bot_control_reader_token is not None
            and self.signal_bot_control_operator_token is not None
            and hmac.compare_digest(
                self.signal_bot_control_reader_token.encode(),
                self.signal_bot_control_operator_token.encode(),
            )
        ):
            raise ValueError("Signal Bot control reader/operator tokens must be distinct")
        _validate_owner_token(
            self.trading_runner_events_reader_token,
            "trading_runner_events_reader_token",
        )
        if (
            self.trading_runner_events_url is None
            and self.trading_runner_events_reader_token is not None
        ):
            raise ValueError(
                "Trading Runner events reader token requires trading_runner_events_url"
            )
        if (
            self.trading_runner_events_url is not None
            and self.trading_runner_events_reader_token is None
        ):
            raise ValueError(
                "Trading Runner events reader token is required when events are enabled"
            )
        _validate_owner_token(
            self.trading_runner_lifecycle_reader_token,
            "trading_runner_lifecycle_reader_token",
        )
        _validate_owner_token(
            self.trading_runner_lifecycle_operator_token,
            "trading_runner_lifecycle_operator_token",
        )
        if self.trading_runner_lifecycle_url is None and (
            self.trading_runner_lifecycle_reader_token is not None
            or self.trading_runner_lifecycle_operator_token is not None
        ):
            raise ValueError(
                "Trading Runner lifecycle downstream tokens require trading_runner_lifecycle_url"
            )
        if self.trading_runner_lifecycle_url is not None and (
            self.trading_runner_lifecycle_reader_token is None
            or self.trading_runner_lifecycle_operator_token is None
        ):
            raise ValueError(
                "Trading Runner lifecycle reader/operator tokens are required when owner control is enabled"
            )
        if (
            self.trading_runner_lifecycle_reader_token is not None
            and self.trading_runner_lifecycle_operator_token is not None
            and hmac.compare_digest(
                self.trading_runner_lifecycle_reader_token.encode(),
                self.trading_runner_lifecycle_operator_token.encode(),
            )
        ):
            raise ValueError("Trading Runner lifecycle reader/operator tokens must be distinct")
        _validate_owner_token(
            self.system_acceptance_reader_token,
            "system_acceptance_reader_token",
        )
        _validate_owner_token(
            self.system_acceptance_operator_token,
            "system_acceptance_operator_token",
        )
        if self.system_acceptance_url is None and (
            self.system_acceptance_reader_token is not None
            or self.system_acceptance_operator_token is not None
        ):
            raise ValueError("System Acceptance downstream tokens require system_acceptance_url")
        if self.system_acceptance_url is not None and self.system_acceptance_reader_token is None:
            raise ValueError(
                "system_acceptance_reader_token is required when Acceptance is enabled"
            )
        if (
            self.system_acceptance_reader_token is not None
            and self.system_acceptance_operator_token is not None
            and hmac.compare_digest(
                self.system_acceptance_reader_token.encode(),
                self.system_acceptance_operator_token.encode(),
            )
        ):
            raise ValueError("System Acceptance reader/operator tokens must be distinct")
        object.__setattr__(
            self,
            "backtest_config_root",
            _optional_existing_directory(self.backtest_config_root, "backtest_config_root"),
        )
        object.__setattr__(
            self,
            "backtest_output_root",
            _managed_path(self.backtest_output_root, "backtest_output_root"),
        )
        object.__setattr__(
            self,
            "backtest_state_path",
            _managed_path(self.backtest_state_path, "backtest_state_path"),
        )
        object.__setattr__(
            self,
            "strategy_factory_state_root",
            _optional_existing_directory(
                self.strategy_factory_state_root,
                "strategy_factory_state_root",
            ),
        )
        object.__setattr__(
            self,
            "research_state_path",
            _optional_existing_file(self.research_state_path, "research_state_path"),
        )
        object.__setattr__(
            self,
            "strategy_audit_state_path",
            _optional_existing_file(
                self.strategy_audit_state_path,
                "strategy_audit_state_path",
            ),
        )
        object.__setattr__(
            self,
            "results_registry_path",
            _optional_existing_file(
                self.results_registry_path,
                "results_registry_path",
            ),
        )
        if self.backtest_config_root is not None and self.operator_token is None:
            raise ValueError("operator_token is required when Backtest Control is enabled")
        if not 1 <= self.backtest_worker_count <= 4:
            raise ValueError("backtest_worker_count must be between 1 and 4")
        if not 0.05 <= self.request_timeout_seconds <= 30.0:
            raise ValueError("request_timeout_seconds must be between 0.05 and 30")
        normalized_log_level = self.log_level.strip().lower()
        if normalized_log_level not in _LOG_LEVELS:
            raise ValueError("log_level must be one of critical/error/warning/info/debug")
        object.__setattr__(self, "log_level", normalized_log_level)


def settings_from_environment(environment: Mapping[str, str] | None = None) -> ServerSettings:
    values = os.environ if environment is None else environment
    config_root_value = values.get("MODULAR_CONTROL_BACKTEST_CONFIG_ROOT")
    strategy_state_root = values.get("MODULAR_CONTROL_STRATEGY_FACTORY_STATE_ROOT")
    research_state_path = values.get("MODULAR_CONTROL_RESEARCH_STATE_PATH")
    strategy_audit_state_path = values.get("MODULAR_CONTROL_STRATEGY_AUDIT_STATE_PATH")
    results_registry_path = values.get("MODULAR_CONTROL_RESULTS_REGISTRY_PATH")
    signal_bot_url = values.get("MODULAR_CONTROL_SIGNAL_BOT_URL")
    signal_bot_control_url = values.get("MODULAR_CONTROL_SIGNAL_BOT_CONTROL_URL")
    trading_runner_events_url = values.get("MODULAR_CONTROL_TRADING_RUNNER_EVENTS_URL")
    trading_runner_lifecycle_url = values.get("MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_URL")
    system_acceptance_url = values.get("MODULAR_CONTROL_SYSTEM_ACCEPTANCE_URL")
    return ServerSettings(
        host=values.get("MODULAR_CONTROL_HOST", "127.0.0.1"),
        port=int(values.get("MODULAR_CONTROL_PORT", "8765")),
        market_data_url=values.get(
            "MODULAR_CONTROL_MARKET_DATA_URL",
            "http://127.0.0.1:8000",
        ),
        market_data_reader_token=_optional_secret(
            values.get("MODULAR_CONTROL_MARKET_DATA_READER_TOKEN")
        ),
        signal_bot_url=(
            signal_bot_url.strip() if signal_bot_url and signal_bot_url.strip() else None
        ),
        signal_bot_control_url=(
            signal_bot_control_url.strip()
            if signal_bot_control_url and signal_bot_control_url.strip()
            else None
        ),
        signal_bot_control_reader_token=_optional_secret(
            values.get("MODULAR_CONTROL_SIGNAL_BOT_CONTROL_READER_TOKEN")
        ),
        signal_bot_control_operator_token=_optional_secret(
            values.get("MODULAR_CONTROL_SIGNAL_BOT_CONTROL_OPERATOR_TOKEN")
        ),
        trading_runner_events_url=(
            trading_runner_events_url.strip()
            if trading_runner_events_url and trading_runner_events_url.strip()
            else None
        ),
        trading_runner_events_reader_token=_optional_secret(
            values.get("MODULAR_CONTROL_TRADING_RUNNER_EVENTS_READER_TOKEN")
        ),
        trading_runner_lifecycle_url=(
            trading_runner_lifecycle_url.strip()
            if trading_runner_lifecycle_url and trading_runner_lifecycle_url.strip()
            else None
        ),
        trading_runner_lifecycle_reader_token=_optional_secret(
            values.get("MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_READER_TOKEN")
        ),
        trading_runner_lifecycle_operator_token=_optional_secret(
            values.get("MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_OPERATOR_TOKEN")
        ),
        system_acceptance_url=(
            system_acceptance_url.strip()
            if system_acceptance_url and system_acceptance_url.strip()
            else None
        ),
        system_acceptance_reader_token=_optional_secret(
            values.get("MODULAR_CONTROL_SYSTEM_ACCEPTANCE_READER_TOKEN")
        ),
        system_acceptance_operator_token=_optional_secret(
            values.get("MODULAR_CONTROL_SYSTEM_ACCEPTANCE_OPERATOR_TOKEN")
        ),
        reader_token=_optional_secret(values.get("MODULAR_CONTROL_READER_TOKEN")),
        operator_token=_optional_secret(values.get("MODULAR_CONTROL_OPERATOR_TOKEN")),
        backtest_config_root=(Path(config_root_value) if config_root_value else None),
        backtest_output_root=Path(
            values.get(
                "MODULAR_CONTROL_BACKTEST_OUTPUT_ROOT",
                str(_DEFAULT_STATE_ROOT / "backtest-results"),
            )
        ),
        backtest_state_path=Path(
            values.get(
                "MODULAR_CONTROL_BACKTEST_STATE_PATH",
                str(_DEFAULT_STATE_ROOT / "backtests.sqlite3"),
            )
        ),
        backtest_worker_count=int(values.get("MODULAR_CONTROL_BACKTEST_WORKERS", "1")),
        strategy_factory_state_root=(Path(strategy_state_root) if strategy_state_root else None),
        research_state_path=(Path(research_state_path) if research_state_path else None),
        strategy_audit_state_path=(
            Path(strategy_audit_state_path) if strategy_audit_state_path else None
        ),
        results_registry_path=(Path(results_registry_path) if results_registry_path else None),
        request_timeout_seconds=float(values.get("MODULAR_CONTROL_REQUEST_TIMEOUT_SECONDS", "2.0")),
        log_level=values.get("MODULAR_CONTROL_LOG_LEVEL", "info"),
    )


def build_backtest_control(settings: ServerSettings) -> BacktestControlService | None:
    if settings.backtest_config_root is None:
        return None
    runner = ManagedBacktestRunner(
        settings.backtest_config_root,
        settings.backtest_output_root,
    )
    store = SQLiteBacktestJobStore(settings.backtest_state_path)
    return BacktestControlService(store, runner)


def build_strategy_control(settings: ServerSettings) -> StrategyControlService | None:
    if settings.strategy_factory_state_root is None:
        return None
    reader = StrategyFactoryStateReader(settings.strategy_factory_state_root)
    return StrategyControlService(reader)


def build_research_control(settings: ServerSettings) -> ResearchControlService | None:
    if settings.research_state_path is None:
        return None
    reader = ResearchRunnerStateReader(settings.research_state_path)
    return ResearchControlService(reader)


def build_evidence_control(settings: ServerSettings) -> EvidenceControlService | None:
    audit_reader = (
        StrategyAuditEvidenceReader(settings.strategy_audit_state_path)
        if settings.strategy_audit_state_path is not None
        else None
    )
    results_reader = (
        ResultsRegistryEvidenceReader(settings.results_registry_path)
        if settings.results_registry_path is not None
        else None
    )
    if audit_reader is None and results_reader is None:
        return None
    return EvidenceControlService(audit_reader=audit_reader, results_reader=results_reader)


def build_market_data_catalog_control(settings: ServerSettings) -> MarketDataCatalogControlService:
    reader = MarketDataHttpCatalogReader(
        settings.market_data_url,
        token=settings.market_data_reader_token,
        timeout_seconds=settings.request_timeout_seconds,
    )
    return MarketDataCatalogControlService(reader)


def build_signal_bot_health_adapter(settings: ServerSettings) -> SignalBotHealthAdapter | None:
    if settings.signal_bot_url is None:
        return None
    reader = SignalBotHttpHealthReader(
        settings.signal_bot_url,
        timeout_seconds=settings.request_timeout_seconds,
    )
    return SignalBotHealthAdapter(reader)


def build_notifications_control(settings: ServerSettings) -> NotificationsControlService | None:
    if settings.signal_bot_control_url is None:
        return None
    reader_token = settings.signal_bot_control_reader_token
    operator_token = settings.signal_bot_control_operator_token
    if reader_token is None or operator_token is None:
        raise ValueError("Signal Bot owner control credentials are required")
    return NotificationsControlService(
        SignalBotControlHttpAdapter(
            settings.signal_bot_control_url,
            reader_token=reader_token,
            operator_token=operator_token,
            timeout_seconds=settings.request_timeout_seconds,
        )
    )


def build_operational_events_control(
    settings: ServerSettings,
) -> OperationalEventsControlService | None:
    if settings.trading_runner_events_url is None:
        return None
    reader_token = settings.trading_runner_events_reader_token
    if reader_token is None:
        raise ValueError("Trading Runner events reader token is required")
    return OperationalEventsControlService(
        TradingRunnerOperationalEventsHttpAdapter(
            settings.trading_runner_events_url,
            reader_token=reader_token,
            timeout_seconds=settings.request_timeout_seconds,
        )
    )


def build_paper_lifecycle_control(settings: ServerSettings) -> PaperLifecycleControlService | None:
    if settings.trading_runner_lifecycle_url is None:
        return None
    reader_token = settings.trading_runner_lifecycle_reader_token
    operator_token = settings.trading_runner_lifecycle_operator_token
    if reader_token is None or operator_token is None:
        raise ValueError("Trading Runner lifecycle owner credentials are required")
    return PaperLifecycleControlService(
        TradingRunnerLifecycleHttpAdapter(
            settings.trading_runner_lifecycle_url,
            reader_token=reader_token,
            operator_token=operator_token,
            timeout_seconds=settings.request_timeout_seconds,
        )
    )


def build_acceptance_control(settings: ServerSettings) -> AcceptanceControlService | None:
    if settings.system_acceptance_url is None:
        return None
    reader_token = settings.system_acceptance_reader_token
    if reader_token is None:
        raise ValueError("System Acceptance reader token is required")
    client = SystemAcceptanceHttpClient(
        settings.system_acceptance_url,
        reader_token=reader_token,
        operator_token=settings.system_acceptance_operator_token,
        timeout_seconds=settings.request_timeout_seconds,
    )
    return AcceptanceControlService(client)


def build_system_resources_control() -> SystemResourcesControlService:
    return SystemResourcesControlService(PsutilSystemResourcesProvider())


def build_observability_control_if_available(
    service: ControlPlaneService,
    resources: SystemResourcesControlService,
    *,
    paper_control: PaperRuntimeControlService | None = None,
) -> ObservabilityControlService | None:
    try:
        return build_observability_control(
            service,
            resources,
            paper_control=paper_control,
        )
    except ObservabilityBackendUnavailable:
        return None


def build_service(
    settings: ServerSettings,
    *,
    backtest_control: BacktestControlService | None = None,
    strategy_control: StrategyControlService | None = None,
    research_control: ResearchControlService | None = None,
    evidence_control: EvidenceControlService | None = None,
    paper_control: PaperRuntimeControlService | None = None,
    acceptance_control: AcceptanceControlService | None = None,
    notifications_control: NotificationsControlService | None = None,
) -> ControlPlaneService:
    market_data_reader = MarketDataHttpHealthReader(
        settings.market_data_url,
        token=settings.market_data_reader_token,
        timeout_seconds=settings.request_timeout_seconds,
    )
    adapters: list[ComponentAdapter] = [MarketDataHealthAdapter(market_data_reader)]
    if notifications_control is not None:
        adapters.append(notifications_control)
    else:
        signal_bot = build_signal_bot_health_adapter(settings)
        if signal_bot is not None:
            adapters.append(signal_bot)
    if backtest_control is not None:
        adapters.append(backtest_control)
    if strategy_control is not None:
        adapters.append(strategy_control)
    if research_control is not None:
        adapters.append(research_control)
    if evidence_control is not None:
        adapters.append(evidence_control)
    if paper_control is not None:
        adapters.append(paper_control)
    if acceptance_control is not None:
        adapters.append(acceptance_control)
    return ControlPlaneService(
        adapters,
        required_components=tuple(ComponentId),
    )


def create_server(
    settings: ServerSettings | None = None,
    *,
    service: ControlPlaneService | None = None,
    backtest_control: BacktestControlService | None = None,
    strategy_control: StrategyControlService | None = None,
    research_control: ResearchControlService | None = None,
    evidence_control: EvidenceControlService | None = None,
    market_data_control: MarketDataCatalogControlService | None = None,
    paper_control: PaperRuntimeControlService | None = None,
    paper_positions_reader: PaperPositionsReader | None = None,
    paper_lifecycle_control: PaperLifecycleControlService | None = None,
    acceptance_control: AcceptanceControlService | None = None,
    notifications_control: NotificationsControlService | None = None,
    operational_events_control: OperationalEventsControlService | None = None,
    system_resources_control: SystemResourcesControlService | None = None,
    observability_control: ObservabilityControlService | None = None,
    recovery_profile_service: RecoveryProfileService | None = None,
) -> FastAPI:
    current = settings or settings_from_environment()
    backtest = backtest_control if backtest_control is not None else build_backtest_control(current)
    strategy = strategy_control if strategy_control is not None else build_strategy_control(current)
    research = research_control if research_control is not None else build_research_control(current)
    evidence = evidence_control if evidence_control is not None else build_evidence_control(current)
    paper_lifecycle = (
        paper_lifecycle_control
        if paper_lifecycle_control is not None
        else build_paper_lifecycle_control(current)
    )
    acceptance = (
        acceptance_control if acceptance_control is not None else build_acceptance_control(current)
    )
    notifications = (
        notifications_control
        if notifications_control is not None
        else build_notifications_control(current)
    )
    operational_events = (
        operational_events_control
        if operational_events_control is not None
        else build_operational_events_control(current)
    )
    market_data = (
        market_data_control
        if market_data_control is not None
        else build_market_data_catalog_control(current)
    )
    system_resources = (
        system_resources_control
        if system_resources_control is not None
        else build_system_resources_control()
    )
    executor = (
        BacktestJobExecutor(backtest, max_workers=current.backtest_worker_count)
        if backtest is not None
        else None
    )
    application_service = service or build_service(
        current,
        backtest_control=backtest,
        strategy_control=strategy,
        research_control=research,
        evidence_control=evidence,
        paper_control=paper_control,
        acceptance_control=acceptance,
        notifications_control=notifications,
    )
    observability = (
        observability_control
        if observability_control is not None
        else build_observability_control_if_available(
            application_service,
            system_resources,
            paper_control=paper_control,
        )
    )
    reader_authorizer = _reader_authorizer(current.reader_token)
    operator_authorizer = _operator_authorizer(current.operator_token)
    application = create_api(
        application_service,
        authorize_reader=reader_authorizer,
        authorize_operator=operator_authorizer,
        backtest_control=backtest,
        submit_backtest=executor.submit if executor is not None else None,
        strategy_control=strategy,
    )
    application.include_router(
        create_market_data_catalog_router(
            market_data,
            authorize_reader=reader_authorizer,
        )
    )
    application.include_router(
        create_research_router(
            research,
            authorize_reader=reader_authorizer,
        )
    )
    application.include_router(
        create_evidence_router(
            evidence,
            authorize_reader=reader_authorizer,
        )
    )
    application.include_router(
        create_paper_router(
            paper_control,
            authorize_reader=reader_authorizer,
            positions_reader=paper_positions_reader,
        )
    )
    application.include_router(
        create_paper_lifecycle_router(
            paper_lifecycle,
            authorize_operator=operator_authorizer,
        )
    )
    application.include_router(
        create_acceptance_router(
            acceptance,
            authorize_reader=reader_authorizer,
            authorize_operator=operator_authorizer,
        )
    )
    application.include_router(
        create_notifications_router(
            notifications,
            authorize_reader=reader_authorizer,
            authorize_operator=operator_authorizer,
        )
    )
    application.include_router(
        create_operational_events_router(
            operational_events,
            authorize_reader=reader_authorizer,
        )
    )
    application.include_router(
        create_system_resources_router(
            system_resources,
            observability_control=observability,
            authorize_reader=reader_authorizer,
        )
    )
    application.include_router(
        create_recovery_profile_router(
            recovery_profile_service,
            authorize_reader=reader_authorizer,
            authorize_operator=operator_authorizer,
        )
    )
    if executor is not None:
        application.state.backtest_executor = executor
        application.router.add_event_handler("startup", executor.recover_queued)
        application.router.add_event_handler("shutdown", executor.shutdown)
    return application


def validate_bind_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in _LOOPBACK_HOSTS:
        raise ValueError("Control Plane API may bind only to a loopback host")
    return normalized


def validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise ValueError("Control Plane API port must be between 1 and 65535")
    return port


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="modular-control-api")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate local Control Plane configuration and exit.",
    )
    args = parser.parse_args(argv)

    environment = dict(os.environ)
    if args.host is not None:
        environment["MODULAR_CONTROL_HOST"] = args.host
    if args.port is not None:
        environment["MODULAR_CONTROL_PORT"] = str(args.port)
    if args.log_level is not None:
        environment["MODULAR_CONTROL_LOG_LEVEL"] = args.log_level
    settings = settings_from_environment(environment)

    if args.check:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "PAPER",
                    "host": settings.host,
                    "port": settings.port,
                    "market_data_url": settings.market_data_url,
                    "market_data_token_configured": (settings.market_data_reader_token is not None),
                    "market_data_catalog_enabled": True,
                    "signal_bot_health_enabled": settings.signal_bot_url is not None,
                    "signal_bot_control_enabled": settings.signal_bot_control_url is not None,
                    "signal_bot_test_notification_enabled": (
                        settings.signal_bot_control_url is not None
                        and settings.signal_bot_control_operator_token is not None
                    ),
                    "structured_operational_events_enabled": (
                        settings.trading_runner_events_url is not None
                    ),
                    "paper_lifecycle_control_enabled": (
                        settings.trading_runner_lifecycle_url is not None
                    ),
                    "paper_lifecycle_mutations_enabled": (
                        settings.trading_runner_lifecycle_url is not None
                        and settings.trading_runner_lifecycle_operator_token is not None
                    ),
                    "system_acceptance_enabled": settings.system_acceptance_url is not None,
                    "system_acceptance_mutations_enabled": (
                        settings.system_acceptance_url is not None
                        and settings.system_acceptance_operator_token is not None
                    ),
                    "reader_authentication_enabled": settings.reader_token is not None,
                    "operator_authentication_enabled": settings.operator_token is not None,
                    "backtest_control_enabled": settings.backtest_config_root is not None,
                    "backtest_worker_count": settings.backtest_worker_count,
                    "strategy_control_enabled": settings.strategy_factory_state_root is not None,
                    "research_control_enabled": settings.research_state_path is not None,
                    "evidence_control_enabled": (
                        settings.strategy_audit_state_path is not None
                        or settings.results_registry_path is not None
                    ),
                    "strategy_audit_evidence_enabled": (
                        settings.strategy_audit_state_path is not None
                    ),
                    "results_registry_evidence_enabled": settings.results_registry_path is not None,
                    "paper_runtime_embedded_only": True,
                    "system_resources_enabled": True,
                    "remote_bind_allowed": False,
                },
                sort_keys=True,
            )
        )
        return 0

    uvicorn.run(
        create_server(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
    return 0


def _reader_authorizer(token: str | None) -> ReaderAuthorizer | None:
    if token is None:
        return None

    expected = f"Bearer {token}".encode()

    def authorize(request: Request) -> None:
        supplied = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, expected):
            raise reader_auth_required()

    return authorize


def _operator_authorizer(token: str | None) -> OperatorAuthorizer | None:
    if token is None:
        return None

    expected = f"Bearer {token}".encode()

    def authorize(request: Request) -> str:
        supplied = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, expected):
            raise operator_auth_required()
        return "local-operator"

    return authorize


def _validate_token(value: str | None, field_name: str) -> None:
    if value is not None and (not value or len(value) > 4096):
        raise ValueError(f"{field_name} must contain 1..4096 characters when configured")


def _validate_owner_token(value: str | None, field_name: str) -> None:
    if value is not None and not 16 <= len(value) <= 4096:
        raise ValueError(f"{field_name} must contain 16..4096 characters when configured")


def _optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _optional_existing_directory(value: Path | None, field_name: str) -> Path | None:
    if value is None:
        return None
    raw = value.expanduser()
    if raw.is_symlink():
        raise ValueError(f"{field_name} must not be a symlink")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise ValueError(f"{field_name} must be an existing directory")
    return resolved


def _optional_existing_file(value: Path | None, field_name: str) -> Path | None:
    if value is None:
        return None
    raw = value.expanduser()
    if raw.is_symlink():
        raise ValueError(f"{field_name} must not be a symlink")
    if not raw.exists() or not raw.is_file():
        raise ValueError(f"{field_name} must be an existing regular file")
    return raw.resolve(strict=True)


def _managed_path(value: Path, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a filesystem path")
    raw = value.expanduser()
    if raw.is_symlink():
        raise ValueError(f"{field_name} must not be a symlink")
    return raw
