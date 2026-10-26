from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_application import (
    ApplicationSettings,
    CreateDatasetExportRequest,
    CreateDownloadRequest,
    MarketDataApplication,
    SQLiteJobStore,
    build_application_context,
)
from market_data_application.contracts import (
    JobEventType,
    JobOperation,
    JobProgress,
    StorageFormat,
)
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState

_BACKTEST_BASELINE_CONFIG_ID = "browser_baseline"
_BACKTEST_CANDIDATE_CONFIG_ID = "browser_candidate"


def request_model() -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
        storage_format=StorageFormat.PARQUET,
        compression="zstd",
        concurrency=1,
    )


def _seed_observable_events(app: MarketDataApplication, job_id: str) -> None:
    """Seed a replay-buffer-sized journal without weakening production durability.

    The synthetic first 1004 progress events share one SQLite transaction so this test fixture
    does not pay one FULL-sync fsync per event on slow self-hosted disks. The final progress
    update still uses the public fenced store API, preserving the real job snapshot, fencing
    semantics and event shape. Production ``record_progress`` and SQLite durability settings
    are intentionally untouched.
    """

    base_timestamp = datetime.now(UTC)
    store = app.context.job_store
    raw_store = SQLiteJobStore(store.path)
    with raw_store._connection(  # noqa: SLF001 - deterministic test-only journal seed
        write=True
    ) as connection:
        for index in range(1, 1_005):
            progress = JobProgress(
                phase=RunState.CREATED,
                determinate=False,
                message=f"browser e2e event {index}",
                counters={"seed_event": index},
            )
            raw_store._append_event(  # noqa: SLF001 - deterministic test-only journal seed
                connection,
                job_id=job_id,
                event_type=JobEventType.PROGRESS,
                state=RunState.CREATED,
                timestamp=base_timestamp + timedelta(microseconds=index),
                payload={"progress": progress.model_dump(mode="json")},
            )

    store.record_progress(
        job_id,
        JobProgress(
            phase=RunState.CREATED,
            determinate=False,
            message="browser e2e event 1005",
            counters={"seed_event": 1_005},
        ),
    )
    snapshot = store.get_job(job_id)
    if snapshot.progress is None or snapshot.progress.counters.get("seed_event") != 1_005:
        raise RuntimeError(
            "observable job snapshot did not retain the final public progress update"
        )


def _observable_event_counts(app: MarketDataApplication, job_id: str) -> tuple[int, int]:
    store = app.context.job_store
    first_page = store.events_after(job_id, after_event_id=0, limit=1_000)
    if len(first_page) != 1_000:
        raise RuntimeError(
            f"observable replay first page must contain 1000 events, got {len(first_page)}"
        )
    tail = store.events_after(job_id, after_event_id=first_page[-1].event_id, limit=1_000)
    total = len(first_page) + len(tail)
    if total != 1_006:
        raise RuntimeError(f"observable replay journal must contain 1006 events, got {total}")
    return len(first_page), total


def _write_backtest_fixture(
    config_root: Path,
    storage_root: Path,
    *,
    dataset_id: str,
    dataset_version: int,
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "browser_backtest_module.py").write_text(
        "\n".join(
            (
                "from modular_core.manifest import ModuleManifest",
                "from modular_core.module_api import MarketView, ModuleDecision, StrategyModule",
                "",
                "",
                "class BrowserBaselineNoopModule(StrategyModule):",
                '    name = "browser_baseline_noop"',
                '    manifest = ModuleManifest(name="browser_baseline_noop")',
                "",
                "    def on_bar(self, view: MarketView) -> ModuleDecision:",
                "        del view",
                "        return ModuleDecision()",
                "",
                "",
                "class BrowserCandidateNoopModule(StrategyModule):",
                '    name = "browser_candidate_noop"',
                '    manifest = ModuleManifest(name="browser_candidate_noop")',
                "",
                "    def on_bar(self, view: MarketView) -> ModuleDecision:",
                "        del view",
                "        return ModuleDecision()",
                "",
            )
        ),
        encoding="utf-8",
    )
    _write_backtest_config(
        config_root,
        storage_root,
        config_id=_BACKTEST_BASELINE_CONFIG_ID,
        module_class="BrowserBaselineNoopModule",
        dataset_id=dataset_id,
        dataset_version=dataset_version,
    )
    _write_backtest_config(
        config_root,
        storage_root,
        config_id=_BACKTEST_CANDIDATE_CONFIG_ID,
        module_class="BrowserCandidateNoopModule",
        dataset_id=dataset_id,
        dataset_version=dataset_version,
    )


def _write_backtest_config(
    config_root: Path,
    storage_root: Path,
    *,
    config_id: str,
    module_class: str,
    dataset_id: str,
    dataset_version: int,
) -> None:
    modules_path = config_root / f"{config_id}-modules.yaml"
    modules_path.write_text(
        f"modules:\n  - class: browser_backtest_module:{module_class}\n",
        encoding="utf-8",
    )
    storage_literal = json.dumps(str(storage_root), ensure_ascii=True)
    (config_root / f"{config_id}.yaml").write_text(
        "\n".join(
            (
                "data:",
                "  symbol: BTCUSDT",
                "  market_type: binance-usdt-m-perpetual",
                "  base_timeframe: 5m",
                "  gap_policy: reject",
                f"  storage_root: {storage_literal}",
                f"  dataset_id: {json.dumps(dataset_id, ensure_ascii=True)}",
                f"  dataset_version: {dataset_version}",
                "  registered_mode: legacy_materialized",
                f"modules: {modules_path.name}",
                "requested_timeframes:",
                "  - 15m",
                "seed: 0",
                "end_of_test_policy: force_close",
                "",
            )
        ),
        encoding="utf-8",
    )


def seed(storage_root: Path, backtest_config_root: Path | None = None) -> dict[str, object]:
    app = MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=storage_root,
                instance_id="browser-e2e-seed",
                worker_count=1,
                artifact_max_bytes=16 * 1024 * 1024,
                artifact_retention_total_bytes=32 * 1024 * 1024,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )
    try:
        created = app.create_download(
            request_model(),
            idempotency_key="browser-e2e-source-dataset",
            actor_id="browser-e2e-seed",
        )
        completed = app.run_job(created.job.job_id)
        if completed.state not in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}:
            raise RuntimeError(f"fake dataset seed failed: {completed.error}")
        if completed.result is None:
            raise RuntimeError("fake dataset seed did not publish a result")
        dataset_id = str(completed.result["dataset"]["dataset_id"])
        dataset = app.get_dataset(dataset_id)

        export_created = app.create_export(
            dataset_id,
            CreateDatasetExportRequest(version=dataset.version),
            idempotency_key="browser-e2e-export",
            actor_id="browser-e2e-seed",
        )
        export_completed = app.run_job(export_created.job.job_id)
        if export_completed.state is not RunState.COMPLETED or export_completed.result is None:
            raise RuntimeError(f"fake export seed failed: {export_completed.error}")

        observable = app.context.job_store.create_job(
            operation=JobOperation.VALIDATE,
            request={"dataset_id": dataset_id, "version": dataset.version, "deep": False},
            idempotency_key="browser-e2e-observable-job",
            correlation_id="corr_browser_e2e_observable",
            actor_id="browser-e2e-seed",
        )
        _seed_observable_events(app, observable.job.job_id)
        first_page_events, total_events = _observable_event_counts(app, observable.job.job_id)

        if backtest_config_root is not None:
            _write_backtest_fixture(
                backtest_config_root,
                storage_root,
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.version,
            )

        artifact_id = str(export_completed.result["artifact_id"])
        artifact = app.get_artifact(artifact_id)
        return {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "instrument_id": dataset.instrument_id,
            "artifact_id": artifact.artifact_id,
            "artifact_filename": artifact.filename,
            "download_job_id": completed.job_id,
            "export_job_id": export_completed.job_id,
            "observable_job_id": observable.job.job_id,
            "observable_first_page_events": first_page_events,
            "observable_total_events": total_events,
            "backtest_baseline_config_id": _BACKTEST_BASELINE_CONFIG_ID,
            "backtest_candidate_config_id": _BACKTEST_CANDIDATE_CONFIG_ID,
        }
    finally:
        app.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backtest-config-root", type=Path)
    args = parser.parse_args()

    storage_root = args.storage_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    backtest_config_root = (
        args.backtest_config_root.expanduser().resolve()
        if args.backtest_config_root is not None
        else None
    )
    storage_root.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = seed(storage_root, backtest_config_root)
    output.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": "ok", "fixture": str(output), **payload}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
