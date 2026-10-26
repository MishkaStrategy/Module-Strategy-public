from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from pydantic import ValidationError

from market_data_ingestion.enums import DataType, MarketType, RunState

from .artifact_contracts import CreateDatasetExportRequest
from .backfill_application import MarketDataApplication
from .backfill_contracts import CreateDatasetBackfillRequest
from .composition import build_application_context
from .contracts import ApiError, ApiErrorDetail, CreateDownloadRequest, JobOperation
from .diagnostics import doctor_response, version_response
from .errors import ApplicationError
from .repair_confirmation_contracts import CreateRepairConfirmationRequest
from .repair_execution_contracts import CreateRepairExecutionRequest
from .settings import ApplicationSettings
from .update_contracts import CreateDatasetUpdateRequest

ApplicationFactory = Callable[[ApplicationSettings], MarketDataApplication]


@dataclass(frozen=True, slots=True)
class CliState:
    storage_root: Path


def _default_factory(settings: ApplicationSettings) -> MarketDataApplication:
    return MarketDataApplication(build_application_context(settings))


def _emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=str))


def _exit_code(error: ApplicationError) -> int:
    if error.code in {
        "JOB_NOT_FOUND",
        "DATASET_NOT_FOUND",
        "EXCHANGE_NOT_FOUND",
        "INSTRUMENT_NOT_FOUND",
        "ARTIFACT_NOT_FOUND",
        "REPAIR_CONFIRMATION_NOT_FOUND",
    }:
        return 4
    if error.code in {
        "IDEMPOTENCY_KEY_CONFLICT",
        "JOB_ALREADY_RUNNING",
        "JOB_LEASE_LOST",
        "JOB_CANCELLATION_CONFLICT",
        "JOB_CANCELLATION_UNAVAILABLE",
        "JOB_BACKEND_IDENTITY_CONFLICT",
        "REPAIR_PREVIEW_STALE",
        "REPAIR_CONFIRMATION_CONFLICT",
        "REPAIR_CONFIRMATION_REJECTED",
        "ARTIFACT_INTEGRITY_FAILED",
        "UPDATE_PARENT_CHANGED",
        "BACKFILL_PARENT_CHANGED",
        "BACKFILL_RANGE_OVERLAP",
    }:
        return 9
    if error.code in {
        "INVALID_JOB_TRANSITION",
        "JOB_OPERATION_UNSUPPORTED",
        "ARTIFACT_LIMIT_EXCEEDED",
        "MARKET_UNSUPPORTED",
        "UPDATE_RANGE_EMPTY",
        "UPDATE_DATA_TYPE_UNAVAILABLE",
        "UPDATE_ACTOR_INVALID",
        "UPDATE_IDEMPOTENCY_KEY_INVALID",
        "BACKFILL_BASE_TIMEFRAME_UNAVAILABLE",
        "BACKFILL_ACTOR_INVALID",
        "BACKFILL_IDEMPOTENCY_KEY_INVALID",
    }:
        return 7
    return 1


def _emit_application_error(error: ApplicationError) -> None:
    correlation_id = str(error.details.get("correlation_id") or f"corr_cli_{uuid4().hex[:20]}")
    _emit(
        ApiError(
            error=ApiErrorDetail(
                code=error.code,
                message=error.message,
                details=error.details,
                correlation_id=correlation_id,
            )
        )
    )


@contextmanager
def _application(
    state: CliState,
    *,
    provided: MarketDataApplication | None,
    factory: ApplicationFactory,
) -> Iterator[MarketDataApplication]:
    if provided is not None:
        yield provided
        return
    application = factory(ApplicationSettings(storage_root=state.storage_root))
    try:
        yield application
    finally:
        application.shutdown()


def create_cli(
    application: MarketDataApplication | None = None,
    *,
    factory: ApplicationFactory = _default_factory,
) -> typer.Typer:
    cli = typer.Typer(
        name="market-data-v1",
        help="Market Data v1 application interface.",
        no_args_is_help=True,
        rich_markup_mode=None,
    )
    jobs = typer.Typer(help="Inspect durable Market Data jobs.", no_args_is_help=True)
    datasets = typer.Typer(help="Inspect immutable registered datasets.", no_args_is_help=True)
    artifacts = typer.Typer(help="Inspect immutable export artifacts.", no_args_is_help=True)
    cli.add_typer(jobs, name="jobs")
    cli.add_typer(datasets, name="datasets")
    cli.add_typer(artifacts, name="artifacts")

    @cli.callback()
    def root(
        ctx: typer.Context,
        storage_root: Annotated[
            Path,
            typer.Option(
                "--storage-root",
                dir_okay=True,
                file_okay=False,
                resolve_path=True,
                help="Local Market Data storage root.",
            ),
        ] = Path("./data"),
    ) -> None:
        ctx.obj = CliState(storage_root=storage_root)

    @cli.command("version")
    def version_command() -> None:
        _emit(version_response())

    @cli.command("doctor")
    def doctor_command(ctx: typer.Context) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                response = doctor_response(current)
                _emit(response)
                if response.status != "ok":
                    raise typer.Exit(code=6)
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @cli.command("exchanges")
    def exchanges_command(ctx: typer.Context) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.list_exchanges())
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @cli.command("capabilities")
    def capabilities_command(
        ctx: typer.Context,
        exchange: Annotated[str, typer.Option("--exchange", min=1, max=64)],
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.get_exchange(exchange))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @cli.command("markets")
    def markets_command(
        ctx: typer.Context,
        exchange: Annotated[str, typer.Option("--exchange", min=1, max=64)],
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.list_markets(exchange))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @cli.command("instruments")
    def instruments_command(
        ctx: typer.Context,
        exchange: Annotated[str, typer.Option("--exchange", min=1, max=64)],
        market: Annotated[str, typer.Option("--market", min=1, max=64)],
        symbol: Annotated[str, typer.Option("--symbol", min=1, max=128)],
    ) -> None:
        state = _state(ctx)
        try:
            selected_market = MarketType(market)
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.resolve_instrument(exchange, selected_market, symbol))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except ValueError as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_MARKET_TYPE",
                        message="instrument market type is invalid",
                        details={"market_type": market},
                        correlation_id=f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @cli.command("download")
    def download_command(
        ctx: typer.Context,
        request_file: Annotated[
            Path,
            typer.Option(
                "--request",
                exists=True,
                dir_okay=False,
                readable=True,
                resolve_path=True,
                help="JSON file containing CreateDownloadRequest.",
            ),
        ],
        idempotency_key: Annotated[
            str,
            typer.Option("--idempotency-key", min=1, max=255),
        ],
        correlation_id: Annotated[
            str | None,
            typer.Option("--correlation-id", max=128),
        ] = None,
        actor_id: Annotated[
            str | None,
            typer.Option("--actor-id", max=128),
        ] = None,
    ) -> None:
        state = _state(ctx)
        try:
            request = CreateDownloadRequest.model_validate_json(request_file.read_text("utf-8"))
            with _application(state, provided=application, factory=factory) as current:
                created = current.create_download(
                    request,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                    actor_id=actor_id,
                )
                result = current.run_job(created.job.job_id)
                _emit(result)
                if result.error is not None:
                    raise typer.Exit(code=3)
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_REQUEST",
                        message="download request could not be parsed or validated",
                        details={"error_type": type(error).__name__},
                        correlation_id=correlation_id or f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("list")
    def datasets_list(
        ctx: typer.Context,
        limit: Annotated[int, typer.Option(min=1, max=200)] = 50,
        offset: Annotated[int, typer.Option(min=0)] = 0,
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.list_datasets(limit=limit, offset=offset))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except ValueError as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_PAGINATION",
                        message="dataset pagination is invalid",
                        details={"error_type": type(error).__name__},
                        correlation_id=f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("show")
    def datasets_show(
        ctx: typer.Context,
        dataset_id: str,
        version: Annotated[int | None, typer.Option(min=1)] = None,
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.get_dataset(dataset_id, version=version))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @datasets.command("validate")
    def datasets_validate(
        ctx: typer.Context,
        dataset_id: str,
        version: Annotated[int | None, typer.Option(min=1)] = None,
        deep: Annotated[bool, typer.Option("--deep")] = False,
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(
                    current.validate_dataset(
                        dataset_id,
                        version=version,
                        deep=deep,
                    )
                )
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @datasets.command("backfill")
    def datasets_backfill(
        ctx: typer.Context,
        dataset_id: str,
        request_file: Annotated[
            Path,
            typer.Option(
                "--request",
                exists=True,
                dir_okay=False,
                readable=True,
                resolve_path=True,
                help="JSON file containing CreateDatasetBackfillRequest.",
            ),
        ],
        idempotency_key: Annotated[
            str,
            typer.Option("--idempotency-key", min=1, max=255),
        ],
        actor_id: Annotated[
            str,
            typer.Option("--actor-id", min=1, max=128),
        ],
        correlation_id: Annotated[
            str | None,
            typer.Option("--correlation-id", max=128),
        ] = None,
    ) -> None:
        state = _state(ctx)
        try:
            request = CreateDatasetBackfillRequest.model_validate_json(
                request_file.read_text("utf-8")
            )
            with _application(state, provided=application, factory=factory) as current:
                created = current.create_backfill(
                    dataset_id,
                    request,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                )
                result = current.run_job(created.job.job_id)
                _emit(result)
                if result.error is not None:
                    raise typer.Exit(code=3)
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_BACKFILL_REQUEST",
                        message="dataset backfill request could not be parsed or validated",
                        details={"error_type": type(error).__name__},
                        correlation_id=correlation_id or f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("update")
    def datasets_update(
        ctx: typer.Context,
        dataset_id: str,
        request_file: Annotated[
            Path,
            typer.Option(
                "--request",
                exists=True,
                dir_okay=False,
                readable=True,
                resolve_path=True,
                help="JSON file containing CreateDatasetUpdateRequest.",
            ),
        ],
        idempotency_key: Annotated[
            str,
            typer.Option("--idempotency-key", min=1, max=255),
        ],
        actor_id: Annotated[
            str,
            typer.Option("--actor-id", min=1, max=128),
        ],
        correlation_id: Annotated[
            str | None,
            typer.Option("--correlation-id", max=128),
        ] = None,
    ) -> None:
        state = _state(ctx)
        try:
            request = CreateDatasetUpdateRequest.model_validate_json(
                request_file.read_text("utf-8")
            )
            with _application(state, provided=application, factory=factory) as current:
                created = current.create_update(
                    dataset_id,
                    request,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                )
                result = current.run_job(created.job.job_id)
                _emit(result)
                if result.error is not None:
                    raise typer.Exit(code=3)
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_UPDATE_REQUEST",
                        message="dataset update request could not be parsed or validated",
                        details={"error_type": type(error).__name__},
                        correlation_id=correlation_id or f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("repair-preview")
    def datasets_repair_preview(
        ctx: typer.Context,
        dataset_id: str,
        version: Annotated[int | None, typer.Option(min=1)] = None,
        data_type: Annotated[str | None, typer.Option("--data-type")] = None,
        deep: Annotated[bool, typer.Option("--deep")] = False,
    ) -> None:
        state = _state(ctx)
        try:
            selected = DataType(data_type) if data_type is not None else None
            with _application(state, provided=application, factory=factory) as current:
                _emit(
                    current.preview_repair(
                        dataset_id,
                        version=version,
                        data_type=selected,
                        deep=deep,
                    )
                )
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except ValueError as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_DATA_TYPE",
                        message="repair preview data type is invalid",
                        details={"error_type": type(error).__name__},
                        correlation_id=f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("repair-confirm")
    def datasets_repair_confirm(
        ctx: typer.Context,
        dataset_id: str,
        request_file: Annotated[
            Path,
            typer.Option(
                "--request",
                exists=True,
                dir_okay=False,
                readable=True,
                resolve_path=True,
                help="JSON file containing CreateRepairConfirmationRequest.",
            ),
        ],
        actor_id: Annotated[
            str,
            typer.Option("--actor-id", min=1, max=128),
        ],
    ) -> None:
        state = _state(ctx)
        try:
            request = CreateRepairConfirmationRequest.model_validate_json(
                request_file.read_text("utf-8")
            )
            with _application(state, provided=application, factory=factory) as current:
                _emit(
                    current.create_repair_confirmation(
                        dataset_id,
                        request,
                        actor_id=actor_id,
                    )
                )
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_REPAIR_CONFIRMATION_REQUEST",
                        message="repair confirmation request could not be parsed or validated",
                        details={"error_type": type(error).__name__},
                        correlation_id=f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("repair-confirmation-show")
    def datasets_repair_confirmation_show(
        ctx: typer.Context,
        confirmation_id: str,
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.get_repair_confirmation(confirmation_id))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @datasets.command("repair-execute")
    def datasets_repair_execute(
        ctx: typer.Context,
        dataset_id: str,
        request_file: Annotated[
            Path,
            typer.Option(
                "--request",
                exists=True,
                dir_okay=False,
                readable=True,
                resolve_path=True,
                help="JSON file containing CreateRepairExecutionRequest.",
            ),
        ],
        idempotency_key: Annotated[
            str,
            typer.Option("--idempotency-key", min=1, max=255),
        ],
        actor_id: Annotated[
            str,
            typer.Option("--actor-id", min=1, max=128),
        ],
        correlation_id: Annotated[
            str | None,
            typer.Option("--correlation-id", max=128),
        ] = None,
    ) -> None:
        state = _state(ctx)
        try:
            request = CreateRepairExecutionRequest.model_validate_json(
                request_file.read_text("utf-8")
            )
            with _application(state, provided=application, factory=factory) as current:
                created = current.create_repair(
                    dataset_id,
                    request,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                )
                result = current.run_job(created.job.job_id)
                _emit(result)
                if result.error is not None:
                    raise typer.Exit(code=3)
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_REPAIR_EXECUTION_REQUEST",
                        message="repair execution request could not be parsed or validated",
                        details={"error_type": type(error).__name__},
                        correlation_id=correlation_id or f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @datasets.command("export")
    def datasets_export(
        ctx: typer.Context,
        dataset_id: str,
        idempotency_key: Annotated[
            str,
            typer.Option("--idempotency-key", min=1, max=255),
        ],
        actor_id: Annotated[
            str,
            typer.Option("--actor-id", min=1, max=128),
        ],
        version: Annotated[int | None, typer.Option(min=1)] = None,
        correlation_id: Annotated[
            str | None,
            typer.Option("--correlation-id", max=128),
        ] = None,
    ) -> None:
        state = _state(ctx)
        try:
            request = CreateDatasetExportRequest(version=version)
            with _application(state, provided=application, factory=factory) as current:
                created = current.create_export(
                    dataset_id,
                    request,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                )
                result = current.run_job(created.job.job_id)
                _emit(result)
                if result.error is not None:
                    raise typer.Exit(code=3)
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except (ValidationError, ValueError) as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_EXPORT_REQUEST",
                        message="dataset export request could not be validated",
                        details={"error_type": type(error).__name__},
                        correlation_id=correlation_id or f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @artifacts.command("show")
    def artifacts_show(ctx: typer.Context, artifact_id: str) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.get_artifact(artifact_id))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @jobs.command("list")
    def jobs_list(
        ctx: typer.Context,
        limit: Annotated[int, typer.Option(min=1, max=200)] = 50,
        offset: Annotated[int, typer.Option(min=0)] = 0,
        state: Annotated[list[str] | None, typer.Option("--state")] = None,
        operation: Annotated[list[str] | None, typer.Option("--operation")] = None,
    ) -> None:
        state_values = tuple(_run_state(value) for value in state or ())
        operation_values = tuple(JobOperation(value) for value in operation or ())
        cli_state = _state(ctx)
        try:
            with _application(cli_state, provided=application, factory=factory) as current:
                _emit(
                    current.list_jobs(
                        limit=limit,
                        offset=offset,
                        states=state_values,
                        operations=operation_values,
                    )
                )
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error
        except ValueError as error:
            _emit(
                ApiError(
                    error=ApiErrorDetail(
                        code="INVALID_FILTER",
                        message="job filter is invalid",
                        details={"error_type": type(error).__name__},
                        correlation_id=f"corr_cli_{uuid4().hex[:20]}",
                    )
                )
            )
            raise typer.Exit(code=2) from error

    @jobs.command("show")
    def jobs_show(ctx: typer.Context, job_id: str) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.get_job(job_id))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @jobs.command("cancel")
    def jobs_cancel(ctx: typer.Context, job_id: str) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                _emit(current.cancel_job(job_id))
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    @jobs.command("events")
    def jobs_events(
        ctx: typer.Context,
        job_id: str,
        after_event_id: Annotated[int, typer.Option(min=0)] = 0,
        limit: Annotated[int, typer.Option(min=1, max=1000)] = 500,
    ) -> None:
        state = _state(ctx)
        try:
            with _application(state, provided=application, factory=factory) as current:
                events = current.events_after(
                    job_id,
                    after_event_id=after_event_id,
                    limit=limit,
                )
                _emit([event.model_dump(mode="json") for event in events])
        except ApplicationError as error:
            _emit_application_error(error)
            raise typer.Exit(code=_exit_code(error)) from error

    return cli


def _state(ctx: typer.Context) -> CliState:
    state = ctx.find_root().obj
    if not isinstance(state, CliState):
        raise RuntimeError("CLI state was not initialized")
    return state


def _run_state(value: str) -> RunState:
    return RunState(value)


app = create_cli()


def main() -> None:
    app()
