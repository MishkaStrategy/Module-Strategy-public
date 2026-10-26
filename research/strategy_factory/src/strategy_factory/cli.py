"""Command-line interface for Strategy Factory lifecycle operations."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .canonical import canonical_json_bytes
from .config import load_request
from .contracts import FactoryMode
from .control import RunControl
from .dataset import DatasetAnalyzer, DatasetValidationPolicy
from .errors import StrategyFactoryError
from .run import StrategyFactoryRun

if TYPE_CHECKING:
    from .application import StrategyFactoryApplication


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strategy-factory")
    parser.add_argument("--state-root", default=".strategy-factory-state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for mode in FactoryMode:
        command = subparsers.add_parser(mode.value)
        command.add_argument("--config", required=True)
        command.add_argument("--commit-sha", required=True)
        command.add_argument("--interval-seconds", type=int, required=True)
        command.add_argument("--minimum-rows", type=int, default=1_000)
        command.add_argument("--minimum-history-days", default="30")
        command.add_argument("--maximum-steps", type=int, default=100)
        command.add_argument("--module-version", action="append", default=[])
    for name in ("status", "resume", "cancel", "inspect"):
        command = subparsers.add_parser(name)
        command.add_argument("--run-id", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--artifact-id", required=True)
    export.add_argument("--output", required=True)
    subparsers.add_parser("doctor")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    application: StrategyFactoryApplication | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(Path(args.state_root))
        if args.command in {mode.value for mode in FactoryMode}:
            return _start(args, application=application)
        control = RunControl(args.state_root, args.run_id)
        if args.command == "status":
            _print(control.status())
            return 0
        if args.command == "inspect":
            _inspect(Path(args.state_root), args.run_id, control.status())
            return 0
        if args.command == "cancel":
            checkpoint = (
                application.cancel(run_id=args.run_id)
                if application is not None
                else control.cancel()
            )
            _print(checkpoint)
            return 0
        if args.command == "resume":
            if application is None:
                _print(control.resume())
                return 0
            result = application.continue_run(run_id=args.run_id)
            _print(_application_result(result))
            return _status_exit_code(result.checkpoint.status.value)
        if args.command == "export":
            return _export(Path(args.state_root), args.run_id, args.artifact_id, args.output)
        parser.error("unsupported command")
    except StrategyFactoryError as exc:
        print(
            json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr
        )
        return 2
    return 2


def _start(
    args: argparse.Namespace,
    *,
    application: StrategyFactoryApplication | None = None,
) -> int:
    from decimal import Decimal

    mode = FactoryMode(args.command)
    request = load_request(args.config, forced_mode=mode)
    if application is not None:
        result = application.start(
            request=request,
            commit_sha=args.commit_sha,
            module_versions=_module_versions(args.module_version),
            maximum_steps=args.maximum_steps,
        )
        _print(_application_result(result))
        return _status_exit_code(result.checkpoint.status.value)
    run = StrategyFactoryRun(
        request=request,
        commit_sha=args.commit_sha,
        state_root=args.state_root,
        module_versions={},
    )
    run.initialize()
    analyzer = DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=args.interval_seconds,
            minimum_rows=args.minimum_rows,
            minimum_history_days=Decimal(args.minimum_history_days),
        )
    )
    analysis = run.run_data_gate(analyzer)
    _print(
        {
            "run_id": run.run_id,
            "status": run.checkpoint.status.value,
            "stage": run.checkpoint.stage.value,
            "dataset_validation": analysis.validation,
            "instrument_profile_created": analysis.profile is not None,
            "next_action": (
                "register modules and generate regime-safe hypotheses"
                if analysis.profile is not None
                else "repair or replace the rejected dataset"
            ),
        }
    )
    return 0 if analysis.profile is not None else 3


def _module_versions(values: list[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in values:
        if not isinstance(raw, str) or "=" not in raw:
            raise StrategyFactoryError("--module-version must use NAME=VERSION")
        name, version = raw.split("=", 1)
        if not name.strip() or not version.strip():
            raise StrategyFactoryError("--module-version requires non-blank NAME and VERSION")
        if name in versions and versions[name] != version:
            raise StrategyFactoryError("conflicting --module-version values")
        versions[name] = version
    return dict(sorted(versions.items()))


def _application_result(result: Any) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "status": result.checkpoint.status.value,
        "stage": result.checkpoint.stage.value,
        "completed": result.completed,
        "steps": tuple(
            {
                "stage": step.stage.value,
                "action": step.action,
                "artifact_id": step.artifact_id,
                "error_type": step.error_type,
            }
            for step in result.steps
        ),
    }


def _status_exit_code(status: str) -> int:
    if status == "COMPLETED":
        return 0
    if status in {"BLOCKED", "PAUSED", "BUDGET_EXHAUSTED"}:
        return 4
    if status == "DATASET_REJECTED":
        return 3
    if status == "CANCELED":
        return 5
    return 2


def _doctor(state_root: Path) -> int:
    checks: dict[str, Any] = {
        "python": {
            "version": platform.python_version(),
            "supported": sys.version_info >= (3, 12),
        },
        "platform": platform.platform(),
        "components": {},
        "state_root": str(state_root.resolve()),
    }
    for module_name in (
        "modular_core",
        "modular_research_runner",
        "modular_results_registry",
        "yaml",
    ):
        try:
            module = __import__(module_name)
            checks["components"][module_name] = {
                "available": True,
                "version": getattr(module, "__version__", "unknown"),
            }
        except ImportError:
            checks["components"][module_name] = {"available": False}
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        probe = state_root / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["state_writable"] = True
    except OSError:
        checks["state_writable"] = False
    checks["strategy_audit"] = {
        "available": False,
        "status": "BLOCKED_EXTERNAL_COMPONENT",
        "contract": "strategy-audit-port-v1",
    }
    healthy = (
        checks["python"]["supported"]
        and checks["state_writable"]
        and all(item["available"] for item in checks["components"].values())
    )
    checks["health"] = (
        "DEGRADED" if not healthy or not checks["strategy_audit"]["available"] else "OK"
    )
    _print(checks)
    return 0 if healthy else 2


def _inspect(state_root: Path, run_id: str, checkpoint: Any) -> None:
    run_root = state_root / run_id
    _print(
        {
            "checkpoint": checkpoint,
            "decision_log": str(run_root / "decisions.jsonl"),
            "artifact_root": str(run_root / "artifacts"),
            "checkpoint_root": str(run_root / "checkpoints"),
        }
    )


def _export(state_root: Path, run_id: str, artifact_id: str, output: str) -> int:
    try:
        artifact_type, digest = artifact_id.rsplit("-", 1)
    except ValueError as exc:
        raise StrategyFactoryError("invalid artifact ID") from exc
    source = state_root / run_id / "artifacts" / artifact_type / f"{digest}.json"
    if not source.is_file():
        raise StrategyFactoryError("artifact not found")
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    _print({"status": "EXPORTED", "source": str(source), "output": str(target)})
    return 0


def _print(value: Any) -> None:
    try:
        raw = canonical_json_bytes(value)
    except StrategyFactoryError:
        raise
    except Exception:
        raw = json.dumps(_fallback(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    print(raw.decode("utf-8"))


def _fallback(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _fallback(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _fallback(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_fallback(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


if __name__ == "__main__":
    raise SystemExit(main())
