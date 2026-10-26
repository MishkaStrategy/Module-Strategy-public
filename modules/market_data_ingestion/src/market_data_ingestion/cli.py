from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import BinanceAdapter, FakeExchangeAdapter
from .adapters.base import ExchangeAdapter
from .doctor import doctor
from .errors import StructuredIngestionError
from .integrity import verify_registered_dataset
from .network import HttpClient
from .pipeline import IngestionService
from .registry import SQLiteDatasetRegistry
from .storage import LocalStorage


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=str))


def _adapters(*, timeout_seconds: float = 30.0, retries: int = 6) -> dict[str, ExchangeAdapter]:
    return {
        "binance": BinanceAdapter(HttpClient(timeout_seconds=timeout_seconds, retries=retries)),
        "fake": FakeExchangeAdapter(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market-data",
        description="Historical Market Data Ingestion",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("exchanges")
    markets = sub.add_parser("markets")
    markets.add_argument("--exchange", required=True)
    capabilities = sub.add_parser("capabilities")
    capabilities.add_argument("--exchange", required=True)
    instruments = sub.add_parser("instruments")
    instruments.add_argument("--exchange", required=True)
    instruments.add_argument("--market", required=True)
    instruments.add_argument("--symbol", required=True)
    download = sub.add_parser("download")
    download.add_argument("--config", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--storage-root", type=Path, required=True)
    inspect.add_argument("--dataset-id", required=True)
    inspect.add_argument("--version", type=int)
    validate = sub.add_parser("validate")
    validate.add_argument("--storage-root", type=Path, required=True)
    validate.add_argument("--dataset-id", required=True)
    validate.add_argument("--version", type=int)
    validate.add_argument("--deep", action="store_true")
    external = sub.add_parser("verify-external-corpus")
    external.add_argument("--spec", type=Path, required=True)
    external.add_argument("--output", type=Path, required=True)
    register_external = sub.add_parser("register-external-corpus")
    register_external.add_argument("--spec", type=Path, required=True)
    register_external.add_argument("--storage-root", type=Path, required=True)
    register_external.add_argument("--output", type=Path, required=True)
    register_external.add_argument("--compression", default="zstd")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--storage-root", type=Path, default=Path("./data"))
    status = sub.add_parser("status")
    status.add_argument("--storage-root", type=Path, required=True)

    update = sub.add_parser("update")
    update.add_argument("--storage-root", type=Path, required=True)
    update.add_argument("--dataset-id", required=True)
    update.add_argument("--version", type=int)
    update.add_argument("--to", required=True)
    update.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    update.add_argument("--compression", default="zstd")

    backfill = sub.add_parser("backfill")
    backfill.add_argument("--storage-root", type=Path, required=True)
    backfill.add_argument("--dataset-id", required=True)
    backfill.add_argument("--version", type=int)
    backfill.add_argument("--data-type", required=True)
    backfill.add_argument("--from", dest="start")
    backfill.add_argument("--to", dest="end")
    backfill.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    backfill.add_argument("--compression", default="zstd")

    repair = sub.add_parser("repair")
    repair.add_argument("--storage-root", type=Path, required=True)
    repair.add_argument("--dataset-id", required=True)
    repair.add_argument("--version", type=int)
    repair.add_argument("--data-type")
    repair.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    repair.add_argument("--compression", default="zstd")

    export = sub.add_parser("export")
    export.add_argument("--storage-root", type=Path, required=True)
    export.add_argument("--dataset-id", required=True)
    export.add_argument("--version", type=int)
    export.add_argument("--target", type=Path, required=True)

    for command in ("resume", "cancel"):
        pending = sub.add_parser(command)
        pending.add_argument("--run-id", required=True)
        pending.add_argument("--storage-root", type=Path, required=True)
    return parser


def _typed_adapters() -> dict[str, Any]:
    return _adapters()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    adapters = _typed_adapters()
    try:
        if args.command == "exchanges":
            _emit(
                [
                    {
                        "exchange": name,
                        "adapter_version": adapter.adapter_version,
                        "maturity": adapter.get_capabilities().maturity.value,
                    }
                    for name, adapter in sorted(adapters.items())
                ]
            )
            return 0
        if args.command in {"markets", "capabilities", "instruments"}:
            adapter = adapters.get(args.exchange)
            if adapter is None:
                _emit({"status": "error", "code": "EXCHANGE_UNSUPPORTED"})
                return 2
            if args.command == "markets":
                _emit([item.value for item in adapter.get_capabilities().markets])
            elif args.command == "capabilities":
                from .canonical import primitive

                _emit(primitive(adapter.get_capabilities()))
            else:
                from .canonical import primitive
                from .enums import MarketType

                _emit(primitive(adapter.resolve_instrument(MarketType(args.market), args.symbol)))
            return 0
        if args.command == "verify-external-corpus":
            from .external_corpus import (
                ExternalCorpusVerifier,
                load_external_corpus_spec,
                write_external_corpus_manifest,
            )

            manifest = ExternalCorpusVerifier().verify(load_external_corpus_spec(args.spec))
            target = write_external_corpus_manifest(args.output, manifest)
            _emit({**manifest.to_payload(), "manifest_path": str(target)})
            return 0
        if args.command == "register-external-corpus":
            from .external_corpus import load_external_corpus_spec
            from .external_registration import (
                ExternalCorpusRegistrar,
                write_external_registration_evidence,
            )

            storage = LocalStorage(args.storage_root)
            registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
            registration_result = ExternalCorpusRegistrar(
                storage=storage,
                registry=registry,
            ).register(
                load_external_corpus_spec(args.spec),
                compression=args.compression,
            )
            target = write_external_registration_evidence(args.output, registration_result)
            _emit({**registration_result.to_payload(), "evidence_path": str(target)})
            return 0
        if args.command == "download":
            from .config import load_config

            config = load_config(args.config)
            configured_adapters = _adapters(
                timeout_seconds=config.download.timeout_seconds,
                retries=config.download.retries,
            )
            storage = LocalStorage(config.storage.root)
            registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
            service = IngestionService(
                adapters=configured_adapters, storage=storage, registry=registry
            )
            result = service.execute(
                config.request,
                format_name=config.storage.format,
                compression=config.storage.compression,
                reject_unresolved_required_gaps=config.validation.reject_unresolved_required_gaps,
                concurrency=config.download.concurrency,
                max_records_in_memory=config.download.max_records_in_memory,
            )
            from .canonical import primitive

            _emit(primitive(result))
            return 0 if result.dataset is not None else 3
        if args.command in {"update", "backfill", "repair"}:
            from .canonical import primitive
            from .enums import DataType
            from .operations import DatasetOperations

            storage = LocalStorage(args.storage_root)
            registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
            service = IngestionService(adapters=adapters, storage=storage, registry=registry)
            operations = DatasetOperations(service=service, storage=storage, registry=registry)
            if args.command == "update":
                update_result = operations.update(
                    args.dataset_id,
                    version=args.version,
                    end=_time(args.to),
                    format_name=args.format,
                    compression=args.compression,
                )
                _emit(primitive(update_result))
                return 0 if update_result.dataset is not None else 3
            if args.command == "backfill":
                backfill_result = operations.backfill(
                    args.dataset_id,
                    version=args.version,
                    data_type=DataType(args.data_type),
                    start=_time(args.start) if args.start else None,
                    end=_time(args.end) if args.end else None,
                    format_name=args.format,
                    compression=args.compression,
                )
                _emit(primitive(backfill_result))
                return 0 if backfill_result.dataset is not None else 3
            repair_result = operations.repair(
                args.dataset_id,
                version=args.version,
                data_type=DataType(args.data_type) if args.data_type else None,
                format_name=args.format,
                compression=args.compression,
            )
            _emit(primitive(repair_result))
            return 0 if repair_result.status.value in {"REPAIRED", "PARTIALLY_REPAIRED"} else 8
        if args.command == "export":
            from .exporting import export_registered_dataset

            registry = SQLiteDatasetRegistry(args.storage_root / "registry.sqlite3")
            descriptor = registry.inspect(args.dataset_id, args.version)
            if descriptor is None:
                _emit({"status": "error", "code": "DATASET_NOT_FOUND"})
                return 4
            report = export_registered_dataset(
                storage_root=args.storage_root,
                descriptor_payload=descriptor,
                target=args.target,
            )
            _emit(report)
            return 0
        if args.command in {"inspect", "validate", "status"}:
            registry = SQLiteDatasetRegistry(args.storage_root / "registry.sqlite3")
            if args.command == "status":
                _emit(registry.health())
                return 0
            descriptor = registry.inspect(args.dataset_id, args.version)
            if descriptor is None:
                _emit({"status": "error", "code": "DATASET_NOT_FOUND"})
                return 4
            if args.command == "inspect":
                _emit(descriptor)
                return 0
            report = verify_registered_dataset(args.storage_root, descriptor, deep=args.deep)
            _emit(report)
            return 0 if report["status"] == "ok" else 5
        if args.command == "resume":
            from .canonical import primitive
            from .run_io import load_resume_spec

            storage = LocalStorage(args.storage_root)
            spec = load_resume_spec(storage.root, args.run_id)
            adapter = adapters.get(spec.request.exchange_id)
            if adapter is None:
                _emit({"status": "error", "code": "EXCHANGE_UNSUPPORTED"})
                return 2
            if adapter.adapter_version != spec.adapter_version:
                _emit(
                    {
                        "status": "error",
                        "code": "ADAPTER_VERSION_MISMATCH",
                        "expected": spec.adapter_version,
                        "actual": adapter.adapter_version,
                    }
                )
                return 9
            storage.clear_cancel(spec.run_id)
            registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
            service = IngestionService(adapters=adapters, storage=storage, registry=registry)
            result = service.execute(
                spec.request,
                format_name=spec.storage_format,
                compression=spec.compression,
                reject_unresolved_required_gaps=(spec.reject_unresolved_required_gaps),
                concurrency=spec.concurrency,
                max_records_in_memory=spec.max_records_in_memory,
                base_dataset=spec.base_dataset,
                base_manifest_override=spec.base_manifest_override,
            )
            _emit(primitive(result))
            return (
                0
                if result.dataset is not None
                else (10 if result.final_state.value == "CANCELLED" else 3)
            )
        if args.command == "cancel":
            from dataclasses import replace

            from .checkpoint import load_checkpoint

            storage = LocalStorage(args.storage_root)
            checkpoint_path = storage.root / "checkpoints" / f"{args.run_id}.json"
            if not checkpoint_path.is_file():
                _emit({"status": "error", "code": "RUN_NOT_FOUND"})
                return 4
            checkpoint = load_checkpoint(checkpoint_path)
            if checkpoint.state.value in {
                "COMPLETED",
                "COMPLETED_WITH_WARNINGS",
                "CANCELLED",
                "FAILED",
                "DATASET_REJECTED",
            }:
                _emit(
                    {
                        "status": "not_cancelled",
                        "code": "RUN_ALREADY_TERMINAL",
                        "state": checkpoint.state.value,
                    }
                )
                return 11
            marker = storage.request_cancel(args.run_id)
            if checkpoint.state.value == "PAUSED":
                from .enums import RunState

                storage.write_checkpoint(
                    args.run_id,
                    replace(
                        checkpoint,
                        state=RunState.CANCELLED,
                        updated_at=datetime.now(UTC),
                    ),
                )
            _emit(
                {
                    "status": "cancel_requested",
                    "run_id": args.run_id,
                    "marker": marker.relative_to(storage.root).as_posix(),
                }
            )
            return 0
        if args.command == "doctor":
            root = args.storage_root.resolve()
            registry = SQLiteDatasetRegistry(root / "registry.sqlite3")
            report = doctor(storage_root=root, registry=registry, adapters=adapters)
            _emit(report)
            return 0 if report["status"] == "ok" else 6
        _emit(
            {
                "status": "not_implemented",
                "command": args.command,
                "message": "command is reserved by the v1 CLI contract but is not release-ready",
            }
        )
        return 7
    except (StructuredIngestionError, ValueError, OSError) as exc:
        if isinstance(exc, StructuredIngestionError):
            _emit({"status": "error", **exc.to_dict()})
        else:
            _emit({"status": "error", "code": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
