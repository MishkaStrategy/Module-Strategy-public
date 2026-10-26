from __future__ import annotations

import argparse

from .client import ControlClient
from .market_data import MarketDatasetList, MarketDatasetSummary, MarketDatasetValidation


def add_dataset_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    dataset = commands.add_parser("dataset", help="Inspect immutable Market Data datasets.")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)

    dataset_list = dataset_commands.add_parser("list", help="List registered immutable datasets.")
    _add_pagination(dataset_list)
    _add_json_flag(dataset_list)

    dataset_show = dataset_commands.add_parser("show", help="Show one sanitized dataset identity.")
    dataset_show.add_argument("dataset_id")
    dataset_show.add_argument("--version", type=int, default=None)
    _add_json_flag(dataset_show)

    dataset_validate = dataset_commands.add_parser(
        "validate",
        help="Read native dataset validation status through the sanitized Control Plane projection.",
    )
    dataset_validate.add_argument("dataset_id")
    dataset_validate.add_argument("--version", type=int, default=None)
    dataset_validate.add_argument("--deep", action="store_true")
    _add_json_flag(dataset_validate)


def run_dataset_command(client: ControlClient, args: argparse.Namespace) -> int:
    if args.dataset_command == "list":
        page = client.datasets(limit=args.limit, offset=args.offset)
        _print_dataset_list(page, as_json=args.json)
        return 0
    if args.dataset_command == "show":
        dataset = client.dataset(args.dataset_id, version=args.version)
        _print_dataset(dataset, as_json=args.json)
        return 0
    if args.dataset_command == "validate":
        validation = client.dataset_validation(
            args.dataset_id,
            version=args.version,
            deep=args.deep,
        )
        _print_validation(validation, as_json=args.json)
        return 0 if validation.status == "ok" else 2
    raise ValueError(f"unsupported dataset command: {args.dataset_command}")


def _print_dataset_list(page: MarketDatasetList, *, as_json: bool) -> None:
    if as_json:
        print(page.model_dump_json(indent=2))
        return
    print(f"DATASETS  visible={len(page.items)} total={page.total} offset={page.offset}")
    if not page.items:
        print("No registered datasets are visible.")
        return
    for dataset in page.items:
        print(
            f"{dataset.dataset_id:<28} v{dataset.version:<4} {dataset.instrument_id:<16} "
            f"{dataset.validation_status:<12} {dataset.start.date()}..{dataset.end.date()}"
        )


def _print_dataset(dataset: MarketDatasetSummary, *, as_json: bool) -> None:
    if as_json:
        print(dataset.model_dump_json(indent=2))
        return
    print(f"DATASET     {dataset.dataset_id}")
    print(f"VERSION     {dataset.version}")
    print(f"HASH        {dataset.dataset_hash}")
    print(f"MARKET      {dataset.exchange_id} / {dataset.market_type}")
    print(f"INSTRUMENT  {dataset.instrument_id}")
    print(f"TIMEFRAMES  {', '.join(dataset.timeframes) if dataset.timeframes else '-'}")
    print(f"RANGE       {dataset.start.isoformat()} .. {dataset.end.isoformat()}")
    print(f"VALIDATION  {dataset.validation_status}")
    print(f"ISSUES      {dataset.unresolved_issue_count}")
    print(f"LINEAGE     {dataset.lineage_count} entries")


def _print_validation(validation: MarketDatasetValidation, *, as_json: bool) -> None:
    if as_json:
        print(validation.model_dump_json(indent=2))
        return
    print(f"DATASET     {validation.dataset_id}")
    print(f"VERSION     {validation.version}")
    print(f"STATUS      {validation.status.upper()} / {validation.validation_status}")
    print(f"DEEP        {validation.deep}")
    print(f"PARTITIONS  {validation.partitions_checked}")
    print(f"RAW         {validation.raw_artifacts_checked}")
    print(f"ROWS        {validation.rows_checked}")
    print(f"ISSUES      {validation.issue_count}")


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit the versioned JSON contract.")


def _add_pagination(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=50, help="Maximum rows to return (1..200).")
    parser.add_argument("--offset", type=int, default=0, help="Zero-based row offset.")
