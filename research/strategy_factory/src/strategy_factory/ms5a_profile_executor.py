"""One-shot scratch-only executor for the bounded MS5A diagnostic proposal.

The parent consumes a separately granted manifest once and starts an accepted-
source child. The child uses the registered Market Data owner and production
Backtest callback, but never opens durable Factory or Research state.
"""

from __future__ import annotations

import argparse
import ast
import cProfile
import faulthandler
import hashlib
import importlib
import importlib.machinery
import inspect
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, timedelta
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO

from .canonical import strict_json_loads
from .errors import CompatibilityError, ExternalComponentUnavailableError
from .ms5a_budget_probe import (
    DIAGNOSTIC_REPAIR_ID,
    DIAGNOSTIC_RUNTIME_FILE_SHA256,
    DIAGNOSTIC_RUNTIME_SHA,
    EXPECTED_RUNTIME_SURFACE_FINGERPRINT,
    FROZEN_LINEAGE_SHA,
    PROTOCOL_ID,
    PROTOCOL_SCHEMA_VERSION,
    SOURCE_SHA,
    WINDOWS,
    FrozenCase,
    _native_specification,
    _read_json,
    authority_manifest,
    select_frozen_cases,
    validate_authority,
)

CHILD_HARD_SECONDS = 120
CHILD_NATIVE_DEADLINE_SECONDS = 105
OUTER_HARD_SECONDS = 20 * 60
RSS_LIMIT_BYTES = 8 * 1024**3
PROFILE_LIMIT_MB = 7 * 1024
_PROFILE_EXECUTION_STOPPED_REASON = "MS5A_PROFILE_EXECUTION_STOPPED_1190"


class _ProfileExecutionStopped(CompatibilityError):
    """Execution is disabled while the controlling STOP remains active."""


def _require_profile_execution_enabled() -> None:
    raise _ProfileExecutionStopped(_PROFILE_EXECUTION_STOPPED_REASON)


BOOTSTRAP_TRACE_SECONDS = 90
# The full frozen path needs 44 events: 2 child-start events, 4 child setup
# events, 14 owner events (including six subphase pairs), 6 plan events, and
# 18 case events. Keep four slots of headroom without changing the byte cap.
BOOTSTRAP_LEDGER_MAX_EVENTS = 48
BOOTSTRAP_LEDGER_MAX_BYTES = 16 * 1024
BOOTSTRAP_TRACE_MAX_BYTES = 256 * 1024
BOOTSTRAP_PROFILE_MAX_BYTES = 64 * 1024 * 1024
_DIAGNOSTIC_PHASES = {
    "child_started",
    "runtime_checkout_verified",
    "native_imports_started",
    "native_imports_completed",
    "selection_completed",
    "owner_resolution_started",
    "owner_call_returned",
    "owner_kline_resolver_started",
    "owner_kline_resolver_returned",
    "owner_funding_resolver_started",
    "owner_funding_resolver_returned",
    "owner_mark_price_resolver_started",
    "owner_mark_price_resolver_returned",
    "owner_kline_overlap_count_started",
    "owner_kline_overlap_count_returned",
    "owner_registered_file_size_started",
    "owner_registered_file_size_returned",
    "owner_funding_mark_merge_started",
    "owner_funding_mark_merge_returned",
    "runtime_attestation_completed",
    "plan_build_started",
    "plan_build_returned",
    "case_started",
    "case_operation_returned",
}
_DIAGNOSTIC_COUNTERS = {
    "owner_kline_resolver_returned": "partitions_processed",
    "owner_funding_resolver_returned": "partitions_processed",
    "owner_mark_price_resolver_returned": "partitions_processed",
    "owner_kline_overlap_count_returned": "rows_processed",
    "owner_registered_file_size_returned": "files_processed",
    "owner_funding_mark_merge_returned": "events_built",
}
_SETUP_FAILURE_PHASES = {"owner_resolution", "runtime_interface"}
_SETUP_FAILURE_REASON_CODES = {
    "runtime_interface": "OWNER_DIAGNOSTIC_INTERFACE_UNSUPPORTED",
}
OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1
_OWNER_DIAGNOSTICS_FACTORY = "create_owner_resolution_diagnostics"
_OWNER_DIAGNOSTICS_MODULE = "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py"
_CONTROLLER_MODULES = {
    "ms5a_budget_probe": "research/strategy_factory/src/strategy_factory/ms5a_budget_probe.py",
    "ms5a_profile_executor": "research/strategy_factory/src/strategy_factory/ms5a_profile_executor.py",
}
_RUNTIME_CONTROLLER_API_MODULES = {
    name: f"research/strategy_factory/src/strategy_factory/{name}.py"
    for name in ("canonical", "errors", "contracts", "contract_codec")
}
_SAFE_EXCEPTION_CLASSES = {
    "AttributeError",
    "CompatibilityError",
    "DatabaseError",
    "DataError",
    "ExternalComponentUnavailableError",
    "FileNotFoundError",
    "ImportError",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "ModuleNotFoundError",
    "NotSupportedError",
    "OperationalError",
    "OSError",
    "OtherError",
    "PermissionError",
    "ProgrammingError",
    "RuntimeError",
    "TimeoutError",
    "TypeError",
    "ValueError",
}


class _ChildPhaseFailure(Exception):
    def __init__(
        self,
        phase: str,
        original: Exception,
        runtime_module_hashes: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.phase = phase
        self.original = original
        self.runtime_module_hashes = dict(runtime_module_hashes or {})


class _DiagnosticLedger:
    """Append-only, bounded scratch breadcrumbs; never a child/case receipt."""

    def __init__(self, scratch_root: Path) -> None:
        self.path = scratch_root / "bootstrap-ledger.jsonl"
        if self.path.is_symlink() or self.path.exists():
            raise CompatibilityError("MS5A bootstrap ledger path is unavailable")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(descriptor, "wb")
        self._started = time.monotonic()
        self._events = 0
        self._bytes = 0
        self._disabled = False

    def record(
        self,
        phase: str,
        counter_name: str | None = None,
        counter_value: int | None = None,
    ) -> None:
        if (
            self._disabled
            or phase not in _DIAGNOSTIC_PHASES
            or self._events >= BOOTSTRAP_LEDGER_MAX_EVENTS
        ):
            return
        if (counter_name is None) != (counter_value is None):
            self._disabled = True
            return
        expected_counter = _DIAGNOSTIC_COUNTERS.get(phase)
        if (
            counter_name != expected_counter
            or isinstance(counter_value, bool)
            or (
                counter_value is not None
                and (not isinstance(counter_value, int) or not 0 <= counter_value <= 2**63 - 1)
            )
        ):
            self._disabled = True
            return
        item: dict[str, str | float | int] = {
            "phase": phase,
            "elapsed_seconds": round(time.monotonic() - self._started, 3),
        }
        if counter_name is not None:
            item["counter_name"] = counter_name
            assert counter_value is not None
            item["counter_value"] = counter_value
        payload = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        if self._bytes + len(payload) > BOOTSTRAP_LEDGER_MAX_BYTES:
            return
        try:
            self._stream.write(payload)
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except OSError:
            self._disabled = True
            return
        self._events += 1
        self._bytes += len(payload)

    def close(self) -> None:
        try:
            self._stream.close()
        except (OSError, RuntimeError, ValueError):
            self._disabled = True


def _start_bootstrap_trace(scratch_root: Path) -> BinaryIO | None:
    path = scratch_root / "bootstrap-trace.txt"
    if path.is_symlink() or path.exists():
        return None
    descriptor: int | None = None
    stream: BinaryIO | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        faulthandler.dump_traceback_later(
            BOOTSTRAP_TRACE_SECONDS, repeat=False, file=stream, exit=False
        )
        return stream
    except (OSError, RuntimeError, ValueError):
        if stream is not None:
            try:
                stream.close()
            except (OSError, RuntimeError, ValueError):
                pass
        elif descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None


def _stop_bootstrap_trace(stream: BinaryIO | None) -> None:
    if stream is None:
        return
    try:
        faulthandler.cancel_dump_traceback_later()
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        stream.close()
    except (OSError, RuntimeError, ValueError):
        pass


def _profile_owner_resolution(
    owner_call: Callable[[], Any],
    profile_path: Path,
    diagnostics: _DiagnosticLedger | None,
) -> Any:
    profiler = cProfile.Profile()
    if diagnostics is not None:
        diagnostics.record("owner_resolution_started")
    profiler.enable()
    try:
        try:
            return owner_call()
        except Exception as exc:
            raise _ChildPhaseFailure("owner_resolution", exc) from exc
    finally:
        profiler.disable()
        try:
            profiler.dump_stats(profile_path)
        except Exception:
            pass
        if diagnostics is not None:
            diagnostics.record("owner_call_returned")


def _require_owner_diagnostic_support(
    owner_module: Any,
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    version = getattr(owner_module, "OWNER_DIAGNOSTICS_INTERFACE_VERSION", None)
    if type(version) is not int or version != OWNER_DIAGNOSTICS_INTERFACE_VERSION:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    factory = getattr(owner_module, _OWNER_DIAGNOSTICS_FACTORY, None)
    owner_builder = getattr(owner_module, "build_ms5a_owner_state", None)
    owner_state_type = getattr(owner_module, "OwnerState", None)
    if not all(callable(item) for item in (factory, owner_builder, owner_state_type)):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    try:
        factory_signature = inspect.signature(factory)
        builder_signature = inspect.signature(owner_builder)
        owner_state_signature = inspect.signature(owner_state_type)
    except (TypeError, ValueError) as exc:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        ) from exc
    if (
        not _is_async_callable(factory)
        and not _is_async_callable(owner_builder)
        and not _is_async_callable(owner_state_type)
        and _signature_supports_call_contract(
            factory_signature,
            positional_count=0,
            keyword_names={"deadline_monotonic", "record"},
        )
        and _signature_supports_call_contract(
            builder_signature,
            positional_count=1,
            keyword_names={"created_at", "diagnostics"},
        )
        and _signature_supports_call_contract(
            owner_state_signature,
            positional_count=0,
            keyword_names={"request", "dataset_file_size", "owner_row_count", "funding_source"},
        )
    ):
        return factory, owner_builder, owner_state_type
    raise ExternalComponentUnavailableError("registered owner does not support bounded diagnostics")


def _require_owner_diagnostics_protocol(diagnostics: Any) -> None:
    contracts = {
        "check_deadline": (0, set()),
        "start": (1, set()),
        "finish": (1, {"counter_name", "counter_value"}),
        "record": (3, set()),
    }
    try:
        for name, (positional_count, keyword_names) in contracts.items():
            method = getattr(diagnostics, name, None)
            if not callable(method) or _is_async_callable(method):
                raise ExternalComponentUnavailableError(
                    "registered owner diagnostics do not implement the bounded protocol"
                )
            signature = inspect.signature(method)
            if not _signature_supports_call_contract(
                signature,
                positional_count=positional_count,
                keyword_names=keyword_names,
            ):
                raise ExternalComponentUnavailableError(
                    "registered owner diagnostics do not implement the bounded protocol"
                )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExternalComponentUnavailableError(
            "registered owner diagnostics do not implement the bounded protocol"
        ) from exc


def _create_owner_diagnostics(
    factory: Callable[..., Any],
    *,
    deadline_monotonic: float,
    record: Callable[..., Any],
    runtime_module_hashes: Mapping[str, str],
) -> Any:
    try:
        diagnostics = factory(deadline_monotonic=deadline_monotonic, record=record)
        _require_owner_diagnostics_protocol(diagnostics)
    except Exception as exc:
        interface_error = ExternalComponentUnavailableError(
            "registered owner diagnostics do not implement the bounded protocol"
        )
        raise _ChildPhaseFailure(
            "runtime_interface", interface_error, runtime_module_hashes
        ) from exc
    return diagnostics


def _require_runtime_owner_diagnostics_source(
    runtime_source: Path, bindings: Mapping[str, Any]
) -> None:
    """Statically negotiate the owner contract before the parent consumes a grant."""

    _read_verified_owner_source(runtime_source, bindings)


def _read_verified_owner_source(
    runtime_source: Path, bindings: Mapping[str, Any]
) -> tuple[Path, bytes, ast.Module]:
    """Read and validate one exact owner source snapshot for later compile/exec."""

    expected_digest = _expected_owner_diagnostics_digest(bindings)
    try:
        resolved_source = runtime_source.resolve(strict=True)
        lexical_owner = resolved_source / _OWNER_DIAGNOSTICS_MODULE
        owner_path = lexical_owner.resolve(strict=True)
        relative_owner = owner_path.relative_to(resolved_source).as_posix()
        if (
            relative_owner != _OWNER_DIAGNOSTICS_MODULE
            or lexical_owner.is_symlink()
            or not owner_path.is_file()
        ):
            raise ValueError("owner module path is outside the runtime checkout")
        source_bytes = owner_path.read_bytes()
        tree = ast.parse(source_bytes, filename=owner_path.name)
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        ) from exc
    if hashlib.sha256(source_bytes).hexdigest() != expected_digest:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    _validate_owner_diagnostics_ast(tree)
    return owner_path, source_bytes, tree


def _validate_owner_diagnostics_ast(tree: ast.Module) -> None:
    version_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "OWNER_DIAGNOSTICS_INTERFACE_VERSION"
    ]
    factories = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == _OWNER_DIAGNOSTICS_FACTORY
    ]
    builders = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "build_ms5a_owner_state"
    ]
    owner_states = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OwnerState"
    ]
    if not all(
        len(nodes) == 1 for nodes in (version_assignments, factories, builders, owner_states)
    ):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    version_assignment = version_assignments[0]
    factory = factories[0]
    builder = builders[0]
    owner_state = owner_states[0]
    if not _ast_owner_interface_bindings_are_stable(
        tree, version_assignment, factory, builder, owner_state
    ):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    try:
        version = ast.literal_eval(version_assignment.value)
    except (ValueError, TypeError):
        version = None
    if (
        type(version) is not int
        or version != OWNER_DIAGNOSTICS_INTERFACE_VERSION
        or not _ast_owner_state_supports_call_contract(
            owner_state,
            tree=tree,
            classvar_aliases=_ast_classvar_aliases(tree, before_line=owner_state.lineno),
            initvar_aliases=_ast_initvar_aliases(tree, before_line=owner_state.lineno),
        )
        or not _ast_supports_call_contract(
            factory,
            positional_count=0,
            keyword_names={"deadline_monotonic", "record"},
        )
        or not _ast_supports_call_contract(
            builder,
            positional_count=1,
            keyword_names={"created_at", "diagnostics"},
        )
    ):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )


def _ast_owner_interface_bindings_are_stable(
    tree: ast.Module,
    version_assignment: ast.Assign,
    factory: ast.FunctionDef | ast.AsyncFunctionDef,
    builder: ast.FunctionDef | ast.AsyncFunctionDef,
    owner_state: ast.ClassDef,
) -> bool:
    interface_names = {
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION",
        _OWNER_DIAGNOSTICS_FACTORY,
        "build_ms5a_owner_state",
        "OwnerState",
    }
    allowed_declarations = {
        id(factory),
        id(builder),
        id(owner_state),
    }
    allowed_store_nodes = {id(version_assignment.targets[0])}
    return (
        not any(
            _ast_module_scope_rebinds_name(
                tree,
                name,
                allowed_declarations=allowed_declarations,
                allowed_store_nodes=allowed_store_nodes,
            )
            for name in interface_names
        )
        and not factory.decorator_list
        and not builder.decorator_list
        and not (_ast_module_scope_mutates_owner_call_signature(tree))
    )


def _ast_module_scope_rebinds_name(
    tree: ast.Module,
    name: str,
    *,
    allowed_declarations: set[int] | None = None,
    allowed_store_nodes: set[int] | None = None,
    allowed_import: ast.Import | ast.ImportFrom | None = None,
) -> bool:
    allowed_declarations = allowed_declarations or set()
    allowed_store_nodes = allowed_store_nodes or set()
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name and id(node) not in allowed_declarations:
                return True
            if any(
                isinstance(child, ast.Global) and name in child.names for child in ast.walk(node)
            ):
                return True
            expressions: list[ast.AST] = list(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                expressions.extend(node.bases)
                expressions.extend(keyword.value for keyword in node.keywords)
            else:
                expressions.extend(node.args.defaults)
                expressions.extend(value for value in node.args.kw_defaults if value is not None)
            pending.extend(expressions)
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and id(node) not in allowed_store_nodes
        ):
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node is allowed_import:
                continue
            for alias in node.names:
                bound_name = alias.asname or (
                    alias.name.split(".", maxsplit=1)[0]
                    if isinstance(node, ast.Import)
                    else alias.name
                )
                if bound_name == name:
                    return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _ast_imported_symbol_is_stable(
    tree: ast.Module,
    *,
    module: str,
    symbol: str,
    local_name: str,
    before_line: int,
) -> bool:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == module
        and any(
            alias.name == symbol and (alias.asname or alias.name) == local_name
            for alias in node.names
        )
    ]
    return (
        len(matches) == 1
        and matches[0].lineno < before_line
        and not _ast_module_scope_rebinds_name(tree, local_name, allowed_import=matches[0])
    )


def _ast_classvar_aliases(tree: ast.Module, *, before_line: int) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                if alias.name in {"typing", "typing_extensions"} and _ast_import_is_stable(
                    tree, node, local_name, before_line
                ):
                    aliases.add(f"{local_name}.ClassVar")
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "typing",
            "typing_extensions",
        }:
            for alias in node.names:
                if alias.name == "ClassVar":
                    local_name = alias.asname or alias.name
                    if _ast_imported_symbol_is_stable(
                        tree,
                        module=node.module,
                        symbol=alias.name,
                        local_name=local_name,
                        before_line=before_line,
                    ):
                        aliases.add(local_name)
    return aliases


def _ast_initvar_aliases(tree: ast.Module, *, before_line: int) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                if alias.name == "dataclasses" and _ast_import_is_stable(
                    tree, node, local_name, before_line
                ):
                    aliases.add(f"{local_name}.InitVar")
        elif isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
            for alias in node.names:
                if alias.name == "InitVar":
                    local_name = alias.asname or alias.name
                    if _ast_imported_symbol_is_stable(
                        tree,
                        module=node.module,
                        symbol=alias.name,
                        local_name=local_name,
                        before_line=before_line,
                    ):
                        aliases.add(local_name)
    return aliases


def _ast_import_is_stable(
    tree: ast.Module,
    import_node: ast.Import,
    local_name: str,
    before_line: int,
) -> bool:
    matching_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import)
        and node.lineno < before_line
        and any(
            (alias.asname or alias.name.split(".", maxsplit=1)[0]) == local_name
            and alias.name in {"typing", "typing_extensions"}
            for alias in node.names
        )
    ]
    return (
        len(matching_imports) == 1
        and matching_imports[0] is import_node
        and not _ast_module_scope_rebinds_name(tree, local_name, allowed_import=import_node)
    )


def _expected_owner_diagnostics_digest(bindings: Mapping[str, Any]) -> str:
    runtime_files = bindings.get("runtime_file_sha256")
    digest = (
        runtime_files.get(_OWNER_DIAGNOSTICS_MODULE) if isinstance(runtime_files, Mapping) else None
    )
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    return digest


def _verify_imported_owner_module(
    owner_module: Any, runtime_source: Path, bindings: Mapping[str, Any]
) -> None:
    module_file = getattr(owner_module, "__file__", None)
    if not isinstance(module_file, str):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    _verify_owner_module_file(Path(module_file), runtime_source, bindings)


def _verify_owner_module_file(
    module_file: Path, runtime_source: Path, bindings: Mapping[str, Any]
) -> None:
    expected_digest = _expected_owner_diagnostics_digest(bindings)
    try:
        resolved_source = runtime_source.resolve(strict=True)
        resolved_file = module_file.resolve(strict=True)
        relative_file = resolved_file.relative_to(resolved_source).as_posix()
        actual_digest = hashlib.sha256(resolved_file.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        ) from exc
    if relative_file != _OWNER_DIAGNOSTICS_MODULE or actual_digest != expected_digest:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )


def _load_verified_owner_module(runtime_source: Path, bindings: Mapping[str, Any]) -> Any:
    module_name = "strategy_factory.multi_strategy_owner"
    if module_name in sys.modules:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    owner_path, source_bytes, _tree = _read_verified_owner_source(runtime_source, bindings)
    try:
        _verify_runtime_strategy_factory_package(runtime_source)
    except (ImportError, OSError, ValueError) as exc:
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        ) from exc
    package = sys.modules.get("strategy_factory")
    if package is None or module_name in sys.modules or hasattr(package, "multi_strategy_owner"):
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    spec = importlib.machinery.ModuleSpec(module_name, loader=None, origin=str(owner_path))
    spec.has_location = True
    owner_module = ModuleType(module_name)
    owner_module.__file__ = str(owner_path)
    owner_module.__cached__ = None
    owner_module.__package__ = "strategy_factory"
    owner_module.__spec__ = spec
    sys.modules[module_name] = owner_module
    try:
        code = compile(source_bytes, str(owner_path), "exec", dont_inherit=True)
        exec(code, owner_module.__dict__)
    except BaseException:
        if sys.modules.get(module_name) is owner_module:
            del sys.modules[module_name]
        if getattr(package, "multi_strategy_owner", None) is owner_module:
            delattr(package, "multi_strategy_owner")
        raise
    if (
        owner_module.__file__ != str(owner_path)
        or owner_module.__spec__ is not spec
        or owner_module.__spec__.origin != str(owner_path)
    ):
        if sys.modules.get(module_name) is owner_module:
            del sys.modules[module_name]
        if getattr(package, "multi_strategy_owner", None) is owner_module:
            delattr(package, "multi_strategy_owner")
        raise ExternalComponentUnavailableError(
            "registered owner does not support bounded diagnostics"
        )
    package.multi_strategy_owner = owner_module
    return owner_module


def _verify_runtime_strategy_factory_package(runtime_source: Path) -> None:
    """Require the imported runtime package to originate in the accepted checkout."""

    package = importlib.import_module("strategy_factory")
    package_file = getattr(package, "__file__", None)
    package_path = getattr(package, "__path__", None)
    if not isinstance(package_file, str) or package_path is None:
        raise ExternalComponentUnavailableError("accepted runtime package is unavailable")
    root = runtime_source.resolve(strict=True)
    source_root = (root / "research/strategy_factory/src").resolve(strict=True)
    resolved_package = Path(package_file).resolve(strict=True)
    if resolved_package != source_root / "strategy_factory/__init__.py":
        raise ExternalComponentUnavailableError("accepted runtime package origin is invalid")
    package_paths = tuple(Path(item).resolve(strict=True) for item in package_path)
    if not package_paths or any(path != source_root / "strategy_factory" for path in package_paths):
        raise ExternalComponentUnavailableError("accepted runtime package origin is invalid")


def _verify_runtime_module_origin(module_name: str, runtime_source: Path) -> None:
    module = sys.modules.get(module_name)
    module_file = getattr(module, "__file__", None) if module is not None else None
    if not isinstance(module_file, str):
        raise ExternalComponentUnavailableError("accepted runtime module is unavailable")
    relative = _RUNTIME_CONTROLLER_API_MODULES.get(module_name.partition(".")[2])
    if module_name == "strategy_factory.multi_strategy_search_surface":
        relative = "research/strategy_factory/src/strategy_factory/multi_strategy_search_surface.py"
    if relative is None:
        raise ExternalComponentUnavailableError("accepted runtime module identity is unknown")
    checkout = runtime_source.resolve(strict=True)
    source_root = (checkout / "research/strategy_factory/src").resolve(strict=True)
    origin = Path(module_file).resolve(strict=True)
    if origin != (checkout / relative).resolve(strict=True) or not origin.is_relative_to(
        source_root
    ):
        raise ExternalComponentUnavailableError("accepted runtime module origin is invalid")


def _is_async_callable(candidate: Any) -> bool:
    if (
        inspect.iscoroutinefunction(candidate)
        or inspect.isasyncgenfunction(candidate)
        or inspect.isgeneratorfunction(candidate)
    ):
        return True
    call_method = inspect.getattr_static(type(candidate), "__call__", None)
    return (
        inspect.iscoroutinefunction(call_method)
        or inspect.isasyncgenfunction(call_method)
        or inspect.isgeneratorfunction(call_method)
    )


def _signature_supports_call_contract(
    signature: inspect.Signature,
    *,
    positional_count: int,
    keyword_names: set[str],
) -> bool:
    parameters = signature.parameters
    keyword_kinds = {
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }
    if any(
        name not in parameters or parameters[name].kind not in keyword_kinds
        for name in keyword_names
    ):
        return False
    try:
        signature.bind(
            *(object() for _ in range(positional_count)),
            **{name: object() for name in keyword_names},
        )
    except TypeError:
        return False
    return True


def _ast_owner_state_supports_call_contract(
    owner_state: ast.ClassDef | None,
    *,
    tree: ast.Module,
    classvar_aliases: set[str],
    initvar_aliases: set[str],
) -> bool:
    if owner_state is None or owner_state.bases or owner_state.keywords:
        return False
    required_names = {
        "request",
        "dataset_file_size",
        "owner_row_count",
        "funding_source",
    }
    dataclass_decorator = _ast_supported_dataclass_decorator(owner_state.decorator_list)
    if dataclass_decorator is False:
        return False
    if dataclass_decorator is not None and not _ast_imported_symbol_is_stable(
        tree,
        module="dataclasses",
        symbol="dataclass",
        local_name="dataclass",
        before_line=owner_state.lineno,
    ):
        return False
    init_methods = [
        node
        for node in owner_state.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__"
    ]
    if (
        len(init_methods) > 1
        or _ast_class_scope_reassigns_name(owner_state, "__init__")
        or _ast_class_scope_defines_or_assigns_name(owner_state, "__new__")
        or _ast_class_scope_defines_or_assigns_name(owner_state, "__signature__")
    ):
        return False
    if dataclass_decorator is None:
        return bool(init_methods) and _ast_supports_call_contract(
            init_methods[0], positional_count=1, keyword_names=required_names
        )
    init_enabled = _ast_dataclass_keyword(dataclass_decorator, "init")
    if init_enabled is not None and not (
        isinstance(init_enabled, ast.Constant) and type(init_enabled.value) is bool
    ):
        return False
    if not init_methods and init_enabled is not None and init_enabled.value is False:
        return False
    if _ast_class_scope_mutates_annotations(owner_state):
        return False
    slots_enabled = _ast_dataclass_keyword(dataclass_decorator, "slots")
    if slots_enabled is not None and not (
        isinstance(slots_enabled, ast.Constant) and type(slots_enabled.value) is bool
    ):
        return False
    if (
        slots_enabled is not None
        and slots_enabled.value is True
        and _ast_class_scope_defines_or_assigns_name(owner_state, "__slots__")
    ):
        return False
    frozen_enabled = _ast_dataclass_keyword(dataclass_decorator, "frozen")
    if frozen_enabled is not None and not (
        isinstance(frozen_enabled, ast.Constant) and type(frozen_enabled.value) is bool
    ):
        return False
    if (
        frozen_enabled is not None
        and frozen_enabled.value is True
        and any(
            _ast_class_scope_defines_or_assigns_name(owner_state, name)
            for name in ("__setattr__", "__delattr__")
        )
    ):
        return False
    order_enabled = _ast_dataclass_keyword(dataclass_decorator, "order")
    if order_enabled is not None and not (
        isinstance(order_enabled, ast.Constant) and type(order_enabled.value) is bool
    ):
        return False
    if (
        order_enabled is not None
        and order_enabled.value is True
        and any(
            _ast_class_scope_defines_or_assigns_name(owner_state, name)
            for name in ("__lt__", "__le__", "__gt__", "__ge__")
        )
    ):
        return False
    unsafe_hash_enabled = _ast_dataclass_keyword(dataclass_decorator, "unsafe_hash")
    if (
        unsafe_hash_enabled is not None
        and unsafe_hash_enabled.value is True
        and _ast_class_scope_defines_or_assigns_name(owner_state, "__hash__")
    ):
        return False
    class_kw_only = _ast_dataclass_keyword(dataclass_decorator, "kw_only")
    if class_kw_only is not None and not (
        isinstance(class_kw_only, ast.Constant) and type(class_kw_only.value) is bool
    ):
        return False
    class_kw_only_value = bool(class_kw_only.value) if class_kw_only is not None else False
    fields: list[inspect.Parameter] = []
    default_value = object()
    saw_positional_default = False
    for node in owner_state.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if _ast_is_classvar_annotation(node.annotation, classvar_aliases):
            if node.value is not None:
                try:
                    ast.literal_eval(node.value)
                except (TypeError, ValueError):
                    return False
            continue
        if not _ast_class_scope_has_single_binding(
            owner_state, node.target.id, expected_binding=node.target
        ):
            return False
        is_initvar = _ast_is_initvar_annotation(node.annotation, initvar_aliases)
        has_default = False
        field_init = True
        field_kw_only: bool | None = None
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "field"
        ):
            if not _ast_imported_symbol_is_stable(
                tree,
                module="dataclasses",
                symbol="field",
                local_name="field",
                before_line=owner_state.lineno,
            ) or _ast_class_scope_reassigns_name(owner_state, "field"):
                return False
            allowed_keywords = {
                "default",
                "default_factory",
                "init",
                "repr",
                "compare",
                "hash",
                "metadata",
                "kw_only",
            }
            keyword_names = [keyword.arg for keyword in node.value.keywords]
            if (
                node.value.args
                or any(name is None or name not in allowed_keywords for name in keyword_names)
                or len(keyword_names) != len(set(keyword_names))
                or {"default", "default_factory"}.issubset(keyword_names)
            ):
                return False
            field_options = {keyword.arg: keyword.value for keyword in node.value.keywords}
            if "init" in field_options:
                init_value = field_options["init"]
                if not (isinstance(init_value, ast.Constant) and type(init_value.value) is bool):
                    return False
                field_init = init_value.value
            if "default" in field_options:
                if not _ast_expression_is_literal(field_options["default"]):
                    return False
                has_default = True
            if "default_factory" in field_options:
                if is_initvar or not _ast_supported_default_factory(
                    field_options["default_factory"]
                ):
                    return False
                has_default = True
            if "metadata" in field_options:
                try:
                    metadata = ast.literal_eval(field_options["metadata"])
                except (TypeError, ValueError):
                    return False
                if not isinstance(metadata, dict):
                    return False
            if "kw_only" in field_options:
                kw_only_value = field_options["kw_only"]
                if not (
                    isinstance(kw_only_value, ast.Constant) and type(kw_only_value.value) is bool
                ):
                    return False
                field_kw_only = kw_only_value.value
        elif node.value is not None:
            if not _ast_expression_is_literal(node.value):
                return False
            has_default = True
        if not field_init:
            continue
        effective_kw_only = class_kw_only_value if field_kw_only is None else field_kw_only
        if not effective_kw_only:
            if not has_default and saw_positional_default:
                return False
            saw_positional_default |= has_default
        fields.append(
            inspect.Parameter(
                node.target.id,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if not has_default else default_value,
            )
        )
    if init_methods:
        return _ast_supports_call_contract(
            init_methods[0], positional_count=1, keyword_names=required_names
        )
    if init_enabled is not None and init_enabled.value is False:
        return False
    try:
        signature = inspect.Signature(fields)
    except (TypeError, ValueError):
        return False
    return required_names.issubset(signature.parameters) and _signature_supports_call_contract(
        signature,
        positional_count=0,
        keyword_names=required_names,
    )


def _ast_supported_dataclass_decorator(
    decorators: list[ast.expr],
) -> ast.Call | None | bool:
    if not decorators:
        return None
    if len(decorators) != 1:
        return False
    decorator = decorators[0]
    if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
        return ast.Call(func=decorator, args=[], keywords=[])
    if not (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "dataclass"
    ):
        return False
    allowed_keywords = {
        "init",
        "repr",
        "eq",
        "order",
        "unsafe_hash",
        "frozen",
        "match_args",
        "kw_only",
        "slots",
        "weakref_slot",
    }
    keyword_names = [keyword.arg for keyword in decorator.keywords]
    if (
        decorator.args
        or any(name is None or name not in allowed_keywords for name in keyword_names)
        or len(keyword_names) != len(set(keyword_names))
    ):
        return False
    for name in ("init", "kw_only"):
        value = _ast_dataclass_keyword(decorator, name)
        if value is not None and not (
            isinstance(value, ast.Constant) and type(value.value) is bool
        ):
            return False
    bool_options = {
        "repr": True,
        "eq": True,
        "order": False,
        "unsafe_hash": False,
        "frozen": False,
        "match_args": True,
        "slots": False,
        "weakref_slot": False,
    }
    for name in bool_options:
        value = _ast_dataclass_keyword(decorator, name)
        if value is None:
            continue
        if not (isinstance(value, ast.Constant) and type(value.value) is bool):
            return False
        bool_options[name] = value.value
    if bool_options["order"] and not bool_options["eq"]:
        return False
    if bool_options["weakref_slot"] and not bool_options["slots"]:
        return False
    return decorator


def _ast_dataclass_keyword(decorator: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in decorator.keywords if keyword.arg == name), None)


def _ast_class_scope_reassigns_name(owner_state: ast.ClassDef, name: str) -> bool:
    pending = list(owner_state.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _ast_class_scope_has_single_binding(
    owner_state: ast.ClassDef,
    name: str,
    *,
    expected_binding: ast.Name,
) -> bool:
    bindings: list[ast.Name | None] = []
    pending = list(owner_state.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    bindings.append(None)
            continue
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            bindings.append(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
                if bound_name == name:
                    bindings.append(None)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    bindings.append(None)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            bindings.append(None)
        elif isinstance(node, ast.MatchAs) and node.name == name:
            bindings.append(None)
        elif isinstance(node, ast.MatchStar) and node.name == name:
            bindings.append(None)
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            bindings.append(None)
        elif isinstance(node, ast.TypeAlias) and node.name.id == name:
            bindings.append(None)
        pending.extend(ast.iter_child_nodes(node))
    return len(bindings) == 1 and bindings[0] is expected_binding


def _ast_class_scope_mutates_annotations(owner_state: ast.ClassDef) -> bool:
    pending = list(owner_state.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Name) and node.id == "__annotations__":
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in {
                "exec",
                "locals",
                "vars",
            }
        ):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _ast_class_scope_defines_or_assigns_name(owner_state: ast.ClassDef, name: str) -> bool:
    pending = list(owner_state.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _ast_module_scope_mutates_owner_call_signature(tree: ast.Module) -> bool:
    attributes = {"__init__", "__new__", "__signature__"}
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(
                isinstance(child, ast.Global) and "OwnerState" in child.names
                for child in ast.walk(node)
            ):
                return True
            expressions: list[ast.AST] = list(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                expressions.extend(node.bases)
                expressions.extend(keyword.value for keyword in node.keywords)
            else:
                expressions.extend(node.args.defaults)
                expressions.extend(value for value in node.args.kw_defaults if value is not None)
            pending.extend(expressions)
            continue
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "OwnerState" and node.attr == "__annotations__":
                return True
            if (
                node.value.id == "OwnerState"
                and node.attr in attributes
                and isinstance(node.ctx, (ast.Store, ast.Del))
            ):
                return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _ast_is_classvar_annotation(annotation: ast.expr, aliases: set[str]) -> bool:
    expression = annotation
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            expression = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    classvar_type: ast.expr = expression
    if isinstance(expression, ast.Subscript):
        classvar_type = expression.value
    return (isinstance(classvar_type, ast.Name) and classvar_type.id in aliases) or (
        isinstance(classvar_type, ast.Attribute) and ast.unparse(classvar_type) in aliases
    )


def _ast_is_initvar_annotation(annotation: ast.expr, aliases: set[str]) -> bool:
    expression = annotation
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            expression = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    initvar_type = expression.value if isinstance(expression, ast.Subscript) else expression
    return (isinstance(initvar_type, ast.Name) and initvar_type.id in aliases) or (
        isinstance(initvar_type, ast.Attribute) and ast.unparse(initvar_type) in aliases
    )


def _ast_expression_is_literal(expression: ast.expr) -> bool:
    try:
        value = ast.literal_eval(expression)
        hash(value)
    except (ValueError, TypeError):
        return False
    return True


def _ast_supported_default_factory(expression: ast.expr) -> bool:
    return isinstance(expression, ast.Name) and expression.id in {
        "bool",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "set",
        "str",
        "tuple",
    }


def _ast_signature(function: ast.FunctionDef | ast.AsyncFunctionDef) -> inspect.Signature | None:
    if (
        isinstance(function, ast.AsyncFunctionDef)
        or function.decorator_list
        or _ast_function_is_generator(function)
    ):
        return None
    arguments = function.args
    positional = [
        *((argument, inspect.Parameter.POSITIONAL_ONLY) for argument in arguments.posonlyargs),
        *((argument, inspect.Parameter.POSITIONAL_OR_KEYWORD) for argument in arguments.args),
    ]
    default_start = len(positional) - len(arguments.defaults)
    parameters: list[inspect.Parameter] = []
    default_value = object()
    for index, (argument, kind) in enumerate(positional):
        default = inspect.Parameter.empty if index < default_start else default_value
        parameters.append(inspect.Parameter(argument.arg, kind, default=default))
    if arguments.vararg is not None:
        parameters.append(inspect.Parameter(arguments.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        parameters.append(
            inspect.Parameter(
                argument.arg,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if default is None else default_value,
            )
        )
    if arguments.kwarg is not None:
        parameters.append(inspect.Parameter(arguments.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    try:
        return inspect.Signature(parameters)
    except (TypeError, ValueError):
        return None


def _ast_function_is_generator(function: ast.FunctionDef) -> bool:
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _ast_supports_call_contract(
    function: ast.FunctionDef | ast.AsyncFunctionDef | None,
    *,
    positional_count: int,
    keyword_names: set[str],
) -> bool:
    if function is None or isinstance(function, ast.AsyncFunctionDef):
        return False
    signature = _ast_signature(function)
    return signature is not None and _signature_supports_call_contract(
        signature,
        positional_count=positional_count,
        keyword_names=keyword_names,
    )


def _trace_frames(data: bytes, source_root: Path) -> list[dict[str, str | int]]:
    try:
        text = data.decode("utf-8", errors="strict")
        root = source_root.resolve(strict=True)
    except (UnicodeError, OSError):
        return []
    frames: list[dict[str, str | int]] = []
    pattern = re.compile(r'^  File "(.+)", line ([0-9]+) in (.+)$', re.MULTILINE)
    for match in pattern.finditer(text):
        try:
            path = Path(match.group(1)).resolve(strict=True)
            relative = path.relative_to(root).as_posix()
            line = int(match.group(2))
        except (OSError, ValueError):
            continue
        function = match.group(3)
        if (
            not relative
            or ".." in Path(relative).parts
            or not 1 <= line <= 10_000_000
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_<>.-]{0,79}", function) is None
        ):
            continue
        frames.append({"source_file": relative, "line": line, "function": function})
        if len(frames) >= 64:
            break
    return frames


def _read_bounded_regular_file(path: Path, maximum_bytes: int) -> bytes | None:
    if maximum_bytes < 0:
        return None
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                return None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _hash_bounded_regular_file(path: Path, maximum_bytes: int) -> str | None:
    if maximum_bytes < 0:
        return None
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                return digest.hexdigest()
            total += len(chunk)
            if total > maximum_bytes:
                return None
            digest.update(chunk)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _collect_bootstrap_diagnostics(scratch_root: Path, source_root: Path) -> dict[str, Any]:
    ledger_path = scratch_root / "bootstrap-ledger.jsonl"
    events: list[dict[str, str | float | int]] = []
    ledger_bytes = _read_bounded_regular_file(ledger_path, BOOTSTRAP_LEDGER_MAX_BYTES)
    if ledger_bytes is not None:
        for raw in ledger_bytes.splitlines(keepends=True)[:BOOTSTRAP_LEDGER_MAX_EVENTS]:
            if not raw.endswith(b"\n"):
                break
            try:
                item = strict_json_loads(raw[:-1].decode("ascii"))
            except (UnicodeError, ValueError, TypeError):
                break
            if (
                not isinstance(item, Mapping)
                or set(item)
                not in (
                    {"phase", "elapsed_seconds"},
                    {"phase", "elapsed_seconds", "counter_name", "counter_value"},
                )
                or not isinstance(item["phase"], str)
                or item["phase"] not in _DIAGNOSTIC_PHASES
                or isinstance(item["elapsed_seconds"], bool)
                or not isinstance(item["elapsed_seconds"], int | float)
                or not math.isfinite(item["elapsed_seconds"])
                or not 0 <= item["elapsed_seconds"] <= CHILD_HARD_SECONDS
            ):
                break
            event: dict[str, str | float | int] = {
                "phase": item["phase"],
                "elapsed_seconds": float(item["elapsed_seconds"]),
            }
            if "counter_name" in item:
                if (
                    item["counter_name"] != _DIAGNOSTIC_COUNTERS.get(item["phase"])
                    or isinstance(item["counter_value"], bool)
                    or not isinstance(item["counter_value"], int)
                    or not 0 <= item["counter_value"] <= 2**63 - 1
                ):
                    break
                event["counter_name"] = item["counter_name"]
                event["counter_value"] = item["counter_value"]
            elif item["phase"] in _DIAGNOSTIC_COUNTERS:
                break
            events.append(event)

    trace_path = scratch_root / "bootstrap-trace.txt"
    trace_hash: str | None = None
    trace_frames: list[dict[str, str | int]] = []
    trace_bytes = _read_bounded_regular_file(trace_path, BOOTSTRAP_TRACE_MAX_BYTES)
    if trace_bytes is not None:
        trace_hash = hashlib.sha256(trace_bytes).hexdigest()
        trace_frames = _trace_frames(trace_bytes, source_root)

    profile_path = scratch_root / "owner-resolution.prof"
    profile_hash = _hash_bounded_regular_file(profile_path, BOOTSTRAP_PROFILE_MAX_BYTES)
    return {
        "evidence_kind": "partial_diagnostic_evidence",
        "last_recorded_phase": events[-1]["phase"] if events else None,
        "phase_events": events,
        "trace_sha256": trace_hash,
        "trace_frames": trace_frames,
        "owner_profile_sha256": profile_hash,
    }


def require_hard_memory_cap(
    *,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    proc_cgroup: Path = Path("/proc/self/cgroup"),
    maximum_bytes: int = RSS_LIMIT_BYTES,
) -> dict[str, int]:
    """Require a finite cgroup-v2 hard memory limit and disabled swap."""
    if sys.platform != "linux":
        raise CompatibilityError("MS5A profile requires Linux cgroup-v2 memory enforcement")
    if maximum_bytes < 1 or cgroup_root.is_symlink() or proc_cgroup.is_symlink():
        raise CompatibilityError("MS5A profile cgroup limit inputs are invalid")
    try:
        root = cgroup_root.resolve(strict=True)
        entries = proc_cgroup.read_text(encoding="utf-8").splitlines()
        unified = next(line[3:] for line in entries if line.startswith("0::"))
        relative = Path(unified.lstrip("/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise CompatibilityError("MS5A profile cgroup path is unsafe")
        group = (root / relative).resolve(strict=True)
        group.relative_to(root)
        memory_limit = int((group / "memory.max").read_text(encoding="ascii").strip())
        swap_limit = int((group / "memory.swap.max").read_text(encoding="ascii").strip())
        current = int((group / "memory.current").read_text(encoding="ascii").strip())
    except CompatibilityError:
        raise
    except (OSError, StopIteration, ValueError) as exc:
        raise CompatibilityError("MS5A profile hard cgroup-v2 memory cap is unavailable") from exc
    if not 0 < memory_limit <= maximum_bytes or swap_limit != 0 or current > memory_limit:
        raise CompatibilityError("MS5A profile requires memory.max <= 8 GiB and memory.swap.max=0")
    return {"memory_max_bytes": memory_limit, "memory_swap_max_bytes": swap_limit}


def require_outer_watchdog(
    *, proc_root: Path = Path("/proc"), timeout_seconds: int = OUTER_HARD_SECONDS
) -> dict[str, str | int]:
    """Attest that a coreutils timeout is PID 1 and wraps this execution CLI."""
    if sys.platform != "linux" or not 1 <= timeout_seconds <= OUTER_HARD_SECONDS:
        raise CompatibilityError("MS5A profile requires a Linux PID-1 outer watchdog")
    try:
        executable = os.readlink(proc_root / "1" / "exe")
        raw_argv = (proc_root / "1" / "cmdline").read_bytes()
        argv = [item.decode("utf-8", errors="strict") for item in raw_argv.split(b"\0") if item]
    except (OSError, UnicodeError) as exc:
        raise CompatibilityError("MS5A profile PID-1 watchdog attestation is unavailable") from exc
    if len(argv) < 14:
        raise CompatibilityError("MS5A profile PID-1 watchdog command is incomplete")
    duration = argv[2] if argv[0] and argv[1] == "--signal=KILL" else ""
    match = re.fullmatch(r"([1-9][0-9]*)s", duration)
    actual_timeout = int(match.group(1)) if match is not None else 0
    if (
        Path(executable).name != "timeout"
        or Path(argv[0]).name != "timeout"
        or match is None
        or actual_timeout > timeout_seconds
        or re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(argv[3]).name) is None
        or argv[4:7] != ["-B", "-m", "strategy_factory.ms5a_profile_executor"]
        or "--plan-only" in argv[7:]
        or "--child" in argv[7:]
    ):
        raise CompatibilityError("MS5A profile PID-1 watchdog command is not approved")
    required_options = {
        "--runtime-root",
        "--runtime-source",
        "--market-data-root",
        "--authority-manifest",
        "--claim-root",
        "--scratch-root",
        "--report",
    }
    command_args = argv[7:]
    if any(command_args.count(option) != 1 for option in required_options):
        raise CompatibilityError("MS5A profile PID-1 watchdog args are not an execution command")
    for option in required_options:
        index = command_args.index(option)
        if index + 1 >= len(command_args) or command_args[index + 1].startswith("-"):
            raise CompatibilityError("MS5A profile PID-1 watchdog args are incomplete")
    return {
        "pid1_executable": "timeout",
        "outer_timeout_seconds": actual_timeout,
        "outer_signal": "KILL",
    }


def _safe_directory(path: Path, *, must_exist: bool) -> Path:
    if path.is_symlink():
        raise CompatibilityError("MS5A probe directory must not be a symlink")
    if must_exist and not path.is_dir():
        raise CompatibilityError("MS5A probe directory is unavailable")
    if not must_exist:
        path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise CompatibilityError("MS5A probe path is not a directory")
    return resolved


def _prepare_scratch(path: Path) -> Path:
    if path.is_symlink():
        raise CompatibilityError("MS5A probe scratch root must not be a symlink")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise CompatibilityError("MS5A probe scratch root must be empty")
    else:
        path.mkdir(parents=True, exist_ok=False)
    return path.resolve(strict=True)


def _verify_runtime_checkout(path: Path) -> Path:
    root = _safe_directory(path, must_exist=True)
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    changes = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if head != DIAGNOSTIC_RUNTIME_SHA or changes.strip():
        raise CompatibilityError("MS5A probe repaired runtime checkout is not exact and clean")
    observed = _runtime_file_sha256(root)
    if observed != DIAGNOSTIC_RUNTIME_FILE_SHA256:
        raise CompatibilityError("MS5A probe repaired runtime source digest is invalid")
    return root


def _runtime_file_sha256(root: Path) -> dict[str, str]:
    observed: dict[str, str] = {}
    resolved_root = root.resolve(strict=True)
    for relative in sorted(DIAGNOSTIC_RUNTIME_FILE_SHA256):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise CompatibilityError("MS5A probe repaired runtime source file is unavailable")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise CompatibilityError("MS5A probe repaired runtime source escaped checkout") from exc
        observed[relative] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return observed


def _verify_controller_checkout(path: Path, expected_sha: str | None = None) -> str:
    root = _safe_directory(path, must_exist=True)
    head = _git_head(root)
    changes = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if (expected_sha is not None and head != expected_sha) or changes.strip():
        raise CompatibilityError("MS5A probe controller checkout is not exact and clean")
    return head


def _bind_execution_identity(
    bindings: Mapping[str, str], runtime_source: Path, controller_root: Path
) -> dict[str, Any]:
    runtime_root = _verify_runtime_checkout(runtime_source)
    controller_sha = _verify_controller_checkout(controller_root)
    return {
        **dict(bindings),
        "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
        "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
        "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
        "runtime_file_sha256": _runtime_file_sha256(runtime_root),
        "runtime_controller_api_sha256": _runtime_controller_api_sha256(runtime_root),
        "controller_commit_sha": controller_sha,
        "controller_module_sha256": _controller_module_sha256(controller_root),
    }


def _runtime_controller_api_sha256(runtime_root: Path) -> dict[str, str]:
    root = runtime_root.resolve(strict=True)
    result: dict[str, str] = {}
    for module, relative in _RUNTIME_CONTROLLER_API_MODULES.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise CompatibilityError("MS5A probe runtime contract module is unavailable")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CompatibilityError("MS5A probe runtime contract module escaped checkout") from exc
        result[module] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return result


def _controller_module_sha256(controller_root: Path) -> dict[str, str]:
    root = controller_root.resolve(strict=True)
    result: dict[str, str] = {}
    for module, relative in _CONTROLLER_MODULES.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise CompatibilityError("MS5A probe controller module is unavailable")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise CompatibilityError("MS5A probe controller module escaped checkout") from exc
        result[module] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return result


def _select(runtime_root: Path) -> tuple[dict[str, str], tuple[FrozenCase, ...]]:
    bindings, cases = select_frozen_cases(runtime_root)
    if bindings.get("runtime_surface_fingerprint") != EXPECTED_RUNTIME_SURFACE_FINGERPRINT:
        raise CompatibilityError("MS5A probe runtime surface changed")
    return bindings, cases


def _consume_claim(claim_root: Path, authority_id: str, manifest_sha256: str) -> Path:
    """Atomically consume a grant; failures never permit retry."""
    root = _safe_directory(claim_root, must_exist=True)
    suffix = hashlib.sha256(authority_id.encode("utf-8")).hexdigest()
    claim = root / "consumed.json"
    descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    payload = json.dumps(
        {"authority_id_sha256": suffix, "manifest_sha256": manifest_sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # O_EXCL is the one-shot boundary. A partial claim still consumes the
        # attempt; never remove it and make a retry possible.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return claim


def _source_import_paths(runtime_root: Path, controller_root: Path | None = None) -> list[str]:
    del controller_root
    paths = [
        runtime_root / "research" / "strategy_factory" / "src",
        runtime_root / "research" / "experiment_runner" / "src",
        runtime_root / "src",
    ]
    for pattern in ("applications/*/src", "modules/*/src", "plugins/*/src", "research/*/src"):
        paths.extend(sorted(runtime_root.glob(pattern)))
    return [str(path.resolve(strict=True)) for path in paths if path.is_dir()]


def _child_command(
    runtime_root: Path,
    controller_root: Path,
    args: Sequence[str],
    controller_module_sha256: Mapping[str, str],
    runtime_controller_api_sha256: Mapping[str, str],
) -> tuple[str, ...]:
    paths = _source_import_paths(runtime_root)
    bootstrap = _controller_bootstrap_source(
        runtime_paths=paths,
        controller_root=controller_root,
        args=args,
        controller_module_sha256=controller_module_sha256,
        runtime_controller_api_sha256=runtime_controller_api_sha256,
    )
    return (sys.executable, "-B", "-c", bootstrap)


def _controller_bootstrap_source(
    *,
    runtime_paths: Sequence[str],
    controller_root: Path,
    args: Sequence[str],
    controller_module_sha256: Mapping[str, str],
    runtime_controller_api_sha256: Mapping[str, str],
    dispatch: bool = True,
) -> str:
    dispatch_source = (
        "from _ms5a_controller.strategy_factory.ms5a_profile_executor import _child_main\n"
        f"raise SystemExit(_child_main({json.dumps(list(args))}))\n"
        if dispatch
        else ""
    )
    return f"""\
import hashlib, importlib.machinery, json, pathlib, sys, types
runtime_paths = {json.dumps(list(runtime_paths))}
controller_root = pathlib.Path({json.dumps(str(controller_root))}).resolve(strict=True)
controller_source_root = (controller_root / "research/strategy_factory/src").resolve(strict=True)
def _is_controller_path(raw):
    try:
        pathlib.Path(raw or ".").resolve(strict=True).relative_to(controller_source_root)
        return True
    except (OSError, ValueError):
        return False
sys.path[:] = [item for item in sys.path if not _is_controller_path(item)]
sys.path[:0] = runtime_paths
manifest = {json.dumps(dict(controller_module_sha256), sort_keys=True)}
expected_modules = {json.dumps(_CONTROLLER_MODULES, sort_keys=True)}
runtime_manifest = {json.dumps(dict(runtime_controller_api_sha256), sort_keys=True)}
runtime_modules = {json.dumps(_RUNTIME_CONTROLLER_API_MODULES, sort_keys=True)}
if set(manifest) != set(expected_modules):
    raise SystemExit("controller source manifest is incomplete")
if set(runtime_manifest) != set(runtime_modules):
    raise SystemExit("runtime API source manifest is incomplete")
package_name = "_ms5a_controller"
package = types.ModuleType(package_name)
package.__package__ = package_name
package.__path__ = []
package.__spec__ = importlib.machinery.ModuleSpec(package_name, loader=None, is_package=True)
package.__spec__.submodule_search_locations = []
sys.modules[package_name] = package
subpackage_name = package_name + ".strategy_factory"
subpackage = types.ModuleType(subpackage_name)
subpackage.__package__ = subpackage_name
subpackage.__path__ = []
subpackage.__spec__ = importlib.machinery.ModuleSpec(subpackage_name, loader=None, is_package=True)
subpackage.__spec__.submodule_search_locations = []
sys.modules[subpackage_name] = subpackage
package.strategy_factory = subpackage
if not runtime_paths:
    raise SystemExit("accepted runtime source paths are unavailable")
runtime_strategy_factory_source = pathlib.Path(runtime_paths[0]).resolve(strict=True)
runtime_checkout = runtime_strategy_factory_source.parents[2]
expected_runtime_source = (runtime_checkout / "research/strategy_factory/src").resolve(strict=True)
if runtime_strategy_factory_source != expected_runtime_source:
    raise SystemExit("accepted runtime strategy_factory source path is invalid")
runtime_source_root = runtime_strategy_factory_source / "strategy_factory"
runtime_package_source = runtime_source_root / "__init__.py"
if runtime_source_root.is_symlink() or runtime_package_source.is_symlink() or not runtime_package_source.is_file():
    raise SystemExit("accepted runtime strategy_factory package is unavailable")
if any(name == "strategy_factory" or name.startswith("strategy_factory.") for name in sys.modules):
    raise SystemExit("strategy_factory was imported before the accepted runtime bootstrap")
_runtime_package = __import__("strategy_factory")
runtime_package_file = pathlib.Path(_runtime_package.__file__).resolve(strict=True)
if runtime_package_file != runtime_package_source.resolve(strict=True):
    raise SystemExit("runtime strategy_factory package origin is invalid")
for short_name, relative in runtime_modules.items():
    module = __import__("strategy_factory." + short_name, fromlist=["*"])
    expected_path = (runtime_checkout / relative).resolve(strict=True)
    module_path = pathlib.Path(module.__file__).resolve(strict=True)
    if module_path != expected_path or hashlib.sha256(module_path.read_bytes()).hexdigest() != runtime_manifest[short_name]:
        raise SystemExit("runtime contract module differs from its bound manifest")
    alias = subpackage_name + "." + short_name
    sys.modules[alias] = module
    setattr(subpackage, short_name, module)
order = ("ms5a_budget_probe", "ms5a_profile_executor")
for short_name in order:
    relative = expected_modules[short_name]
    lexical = controller_root / relative
    path = lexical.resolve(strict=True)
    if lexical.is_symlink() or path.relative_to(controller_root).as_posix() != relative or not path.is_file():
        raise SystemExit("controller source path is outside its checked checkout")
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != manifest[short_name]:
        raise SystemExit("controller source differs from its bound manifest")
    name = subpackage_name + "." + short_name
    spec = importlib.machinery.ModuleSpec(name, loader=None, origin=str(path))
    spec.has_location = True
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__cached__ = None
    module.__package__ = subpackage_name
    module.__spec__ = spec
    sys.modules[name] = module
    setattr(subpackage, short_name, module)
    try:
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        if sys.modules.get(name) is module:
            del sys.modules[name]
        raise
{dispatch_source}"""


def _process_group_rss_bytes(pid: int) -> int:
    result = subprocess.run(
        ("ps", "-axo", "pid=,pgid=,rss="),
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    total_kib = 0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            _, group_id, rss_kib = (int(part) for part in parts)
        except ValueError:
            continue
        if group_id == pid:
            total_kib += rss_kib
    return total_kib * 1024


def _kill_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def launch_bounded_child(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int = CHILD_HARD_SECONDS,
    memory_limit_bytes: int = RSS_LIMIT_BYTES,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    rss_reader: Callable[[int], int] = _process_group_rss_bytes,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int | None, str, str]:
    """Run one child and terminate its process group at either hard cap."""
    started = monotonic()
    process = popen(
        tuple(command),
        cwd=cwd,
        env=dict(environment),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[str] = []
    while process.poll() is None:
        if monotonic() - started >= timeout_seconds:
            _kill_group(process)
            return "hard_timeout", process.returncode, "CHILD_WALL_TIMEOUT", ""
        try:
            if rss_reader(process.pid) > memory_limit_bytes:
                _kill_group(process)
                return "memory_limit", process.returncode, "CHILD_RSS_LIMIT", ""
        except (OSError, subprocess.SubprocessError, ValueError):
            _kill_group(process)
            return "watchdog_error", process.returncode, "RSS_WATCHDOG_UNAVAILABLE", ""
        for key, _ in selector.select(timeout=0.1):
            chunk = os.read(key.fd, 4096)
            if chunk:
                output.append(chunk.decode("utf-8", errors="replace"))
        sleep(0.05)
    for key, _ in selector.select(timeout=0):
        chunk = os.read(key.fd, 4096)
        if chunk:
            output.append(chunk.decode("utf-8", errors="replace"))
    selector.close()
    if process.returncode:
        reason = _child_reason_code("".join(output))
        return "child_failed", process.returncode, reason, "".join(output)
    return "completed", 0, "", "".join(output)


def _safe_exception_class(exc: BaseException) -> str:
    name = type(exc).__name__
    module = type(exc).__module__
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if module == "strategy_factory.errors" and name in {
        "CompatibilityError",
        "ExternalComponentUnavailableError",
    }:
        return name
    if module in {"sqlite3", "sqlite3.dbapi2"} and name in {
        "DatabaseError",
        "DataError",
        "IntegrityError",
        "InterfaceError",
        "InternalError",
        "NotSupportedError",
        "OperationalError",
        "ProgrammingError",
    }:
        return name
    if module == "builtins" and name in {
        "AttributeError",
        "FileNotFoundError",
        "ImportError",
        "ModuleNotFoundError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }:
        return name
    return "OtherError"


def _receipt_identity(bindings: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(bindings or {})
    runtime_files = values.get("runtime_file_sha256", dict(DIAGNOSTIC_RUNTIME_FILE_SHA256))
    return {
        "frozen_lineage_source_commit_sha": values.get(
            "frozen_lineage_source_commit_sha", FROZEN_LINEAGE_SHA
        ),
        "runtime_source_commit_sha": values.get(
            "runtime_source_commit_sha", DIAGNOSTIC_RUNTIME_SHA
        ),
        "runtime_repair_id": values.get("runtime_repair_id", DIAGNOSTIC_REPAIR_ID),
        "runtime_file_sha256": runtime_files,
        "controller_commit_sha": values.get("controller_commit_sha", "0" * 40),
    }


def _setup_failure_payload(
    exc: _ChildPhaseFailure,
    bindings: Mapping[str, Any] | None = None,
    runtime_module_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if exc.phase not in _SETUP_FAILURE_PHASES:
        raise CompatibilityError("MS5A child setup phase is not allowlisted")
    phase = exc.phase
    root_error = exc.original
    error_class = _safe_exception_class(root_error)
    causes: list[str] = []
    current = root_error.__cause__ or root_error.__context__
    seen = {id(root_error)}
    while current is not None and len(causes) < 4 and id(current) not in seen:
        seen.add(id(current))
        causes.append(_safe_exception_class(current))
        current = current.__cause__ or current.__context__
    failure = {
        "phase": phase,
        "dataset_kind": "ohlcv_funding_mark_price" if phase == "owner_resolution" else None,
        "error_class": error_class,
        "cause_classes": causes,
    }
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "probe": PROTOCOL_ID,
        "status": "setup_failure",
        "reason_code": _SETUP_FAILURE_REASON_CODES.get(phase, error_class.upper()),
        **_receipt_identity(bindings),
        "runtime_module_sha256": dict(runtime_module_hashes or exc.runtime_module_hashes),
        "cases": [],
        "setup_failure": failure,
        "candidate_metrics_included": False,
    }


def _child_reason_code(stdout: str) -> str:
    try:
        payload = strict_json_loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError, TypeError):
        return "CHILD_FAILED_WITHOUT_RECEIPT"
    reason = payload.get("reason_code") if isinstance(payload, Mapping) else None
    return reason if isinstance(reason, str) and reason.isupper() else "CHILD_FAILED"


def _sanitized_child_receipt(
    stdout: str, expected_bindings: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    try:
        payload = strict_json_loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError, TypeError) as exc:
        raise CompatibilityError("MS5A probe child receipt is unavailable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("probe") != PROTOCOL_ID
        or payload.get("schema_version") != PROTOCOL_SCHEMA_VERSION
    ):
        raise CompatibilityError("MS5A probe child receipt identity is invalid")
    identity = _receipt_identity(expected_bindings)
    for name, expected in identity.items():
        observed = payload.get(name)
        if expected_bindings is None and name == "controller_commit_sha":
            continue
        if observed != expected:
            raise CompatibilityError("MS5A probe child lineage/runtime attestation is invalid")
    if (
        payload.get("frozen_lineage_source_commit_sha") != FROZEN_LINEAGE_SHA
        or payload.get("runtime_source_commit_sha") != DIAGNOSTIC_RUNTIME_SHA
        or payload.get("runtime_repair_id") != DIAGNOSTIC_REPAIR_ID
        or payload.get("runtime_file_sha256") != DIAGNOSTIC_RUNTIME_FILE_SHA256
        or not isinstance(payload.get("controller_commit_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["controller_commit_sha"]) is None
    ):
        raise CompatibilityError("MS5A probe child source bindings are invalid")
    module_hashes = payload.get("runtime_module_sha256")
    if not isinstance(module_hashes, Mapping) or any(
        not isinstance(name, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for name, digest in module_hashes.items()
    ):
        raise CompatibilityError("MS5A probe runtime module attestation is malformed")
    setup_failure = payload.get("setup_failure")
    runtime_interface_failure = (
        payload.get("status") == "setup_failure"
        and isinstance(setup_failure, Mapping)
        and setup_failure.get("phase") == "runtime_interface"
    )
    expected_repaired_modules = {
        "market_data_ingestion.integrity": (
            "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
        ),
        "market_data_ingestion.storage": (
            "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
        ),
    }
    if runtime_interface_failure and set(module_hashes) not in (
        set(),
        set(expected_repaired_modules),
    ):
        raise CompatibilityError(
            "MS5A probe runtime interface failure has unexpected module imports"
        )
    for module_name, relative in (
        () if runtime_interface_failure and not module_hashes else expected_repaired_modules.items()
    ):
        if module_hashes.get(module_name) != DIAGNOSTIC_RUNTIME_FILE_SHA256[relative]:
            raise CompatibilityError("MS5A probe repaired runtime module attestation is missing")
    if payload.get("status") == "setup_failure":
        if (
            set(payload)
            != {
                "probe",
                "status",
                "reason_code",
                "schema_version",
                "frozen_lineage_source_commit_sha",
                "runtime_source_commit_sha",
                "runtime_repair_id",
                "runtime_file_sha256",
                "controller_commit_sha",
                "runtime_module_sha256",
                "cases",
                "setup_failure",
                "candidate_metrics_included",
            }
            or payload.get("schema_version") != PROTOCOL_SCHEMA_VERSION
            or payload.get("candidate_metrics_included") is not False
        ):
            raise CompatibilityError("MS5A probe setup failure receipt has unexpected fields")
        if payload.get("cases") != []:
            raise CompatibilityError("MS5A probe setup failure must not claim case evidence")
        failure = payload.get("setup_failure")
        if not isinstance(failure, Mapping) or set(failure) != {
            "phase",
            "dataset_kind",
            "error_class",
            "cause_classes",
        }:
            raise CompatibilityError("MS5A probe setup failure metadata is malformed")
        phase = failure["phase"]
        dataset_kind = failure["dataset_kind"]
        error_class = failure["error_class"]
        causes = failure["cause_classes"]
        if (
            not isinstance(phase, str)
            or phase not in _SETUP_FAILURE_PHASES
            or dataset_kind != ("ohlcv_funding_mark_price" if phase == "owner_resolution" else None)
            or not isinstance(error_class, str)
            or error_class not in _SAFE_EXCEPTION_CLASSES
            or payload.get("reason_code")
            != _SETUP_FAILURE_REASON_CODES.get(phase, error_class.upper())
            or not isinstance(causes, list)
            or len(causes) > 4
            or any(
                not isinstance(cause, str) or cause not in _SAFE_EXCEPTION_CLASSES
                for cause in causes
            )
        ):
            raise CompatibilityError("MS5A probe setup failure metadata is invalid")
        return {
            "cases": [],
            "runtime_module_sha256": dict(module_hashes),
            "setup_failure": dict(failure),
        }
    if payload.get("status") not in {"completed", "stopped"} or "setup_failure" in payload:
        raise CompatibilityError("MS5A probe child status is invalid")
    if set(payload) != {
        "schema_version",
        "probe",
        "status",
        "reason_code",
        "frozen_lineage_source_commit_sha",
        "runtime_source_commit_sha",
        "runtime_repair_id",
        "runtime_file_sha256",
        "controller_commit_sha",
        "runtime_module_sha256",
        "cases",
        "candidate_metrics_included",
    }:
        raise CompatibilityError("MS5A probe child receipt has unexpected fields")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise CompatibilityError("MS5A probe child receipt cases are malformed")
    allowed_families = {"breakout_return", "fvg_rt", "liquidity_sweep"}
    expected_keys = {
        "family",
        "bars",
        "stage_role",
        "status",
        "reason_code",
        "duration_seconds",
        "input_sha256",
        "result_tree_sha256",
        "profile_sha256",
    }
    sanitized: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, Mapping) or set(case) != expected_keys:
            raise CompatibilityError("MS5A probe child receipt has unexpected fields")
        if (
            case["family"] not in allowed_families
            or case["bars"] not in WINDOWS
            or case["stage_role"] != "optimization_000"
            or case["status"] not in {"completed", "failed"}
            or not isinstance(case["duration_seconds"], int | float)
            or isinstance(case["duration_seconds"], bool)
        ):
            raise CompatibilityError("MS5A probe child receipt case binding is invalid")
        for name in ("input_sha256", "result_tree_sha256", "profile_sha256"):
            digest = case[name]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise CompatibilityError("MS5A probe child receipt digest is invalid")
        sanitized.append(dict(case))
    expected = [
        (family, bars)
        for bars in WINDOWS
        for family in ("breakout_return", "fvg_rt", "liquidity_sweep")
    ]
    if [(item["family"], item["bars"]) for item in sanitized] != expected[: len(sanitized)]:
        raise CompatibilityError("MS5A probe child receipt case order/coverage is invalid")
    if payload.get("status") == "completed" and (
        len(sanitized) != len(expected) or any(item["status"] != "completed" for item in sanitized)
    ):
        raise CompatibilityError("MS5A probe child claims completion without all cases")
    return {"cases": sanitized, "runtime_module_sha256": dict(module_hashes)}


def _attest_runtime_modules(runtime_root: Path, surface: Any) -> dict[str, str]:
    module_names = {
        "modular_backtest",
        "modular_backtest.runner",
        "modular_backtest.scalability.config",
        "strategy_factory.multi_strategy_owner",
        "strategy_factory.multi_strategy_research_runtime",
        "strategy_factory.owner_runtime",
        "strategy_factory.backtest_config",
        "strategy_factory.multi_strategy_runtime_closure",
        "strategy_factory.multi_strategy_search_surface",
        "strategy_factory.run",
        "modular_research_runner",
        "market_data_ingestion.dataset_reader",
        "market_data_ingestion.funding_dataset_reader",
        "market_data_ingestion.integrity",
        "market_data_ingestion.price_dataset_reader",
        "market_data_ingestion.required_gap_policy",
        "market_data_ingestion.storage",
    }
    for trusted in surface.trusted_runtime_modules:
        module_names.add(trusted.import_path.partition(":")[0])
    digests: dict[str, str] = {}
    for name in sorted(module_names):
        module = importlib.import_module(name)
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise CompatibilityError("MS5A probe runtime module has no source file")
        path = Path(module_file).resolve(strict=True)
        try:
            path.relative_to(runtime_root)
        except ValueError as exc:
            raise CompatibilityError(
                "MS5A probe imported a module outside accepted source"
            ) from exc
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for module_name, relative in (
        (
            "market_data_ingestion.integrity",
            "modules/market_data_ingestion/src/market_data_ingestion/integrity.py",
        ),
        (
            "market_data_ingestion.storage",
            "modules/market_data_ingestion/src/market_data_ingestion/storage.py",
        ),
    ):
        if digests.get(module_name) != DIAGNOSTIC_RUNTIME_FILE_SHA256[relative]:
            raise CompatibilityError("MS5A probe repaired runtime module digest is invalid")
    return digests


def _attest_repaired_mdi_modules(runtime_root: Path) -> dict[str, str]:
    """Attest the two repaired modules before calling the registered owner."""

    resolved_root = runtime_root.resolve(strict=True)
    modules = {
        "market_data_ingestion.integrity": (
            "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
        ),
        "market_data_ingestion.storage": (
            "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
        ),
    }
    digests: dict[str, str] = {}
    for module_name, relative in modules.items():
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise CompatibilityError("MS5A probe repaired runtime module has no source file")
        path = Path(module_file).resolve(strict=True)
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise CompatibilityError(
                "MS5A probe imported a repaired runtime module outside the candidate"
            ) from exc
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != DIAGNOSTIC_RUNTIME_FILE_SHA256[relative]:
            raise CompatibilityError("MS5A probe repaired runtime module digest is invalid")
        digests[module_name] = digest
    return digests


def _child_receipt_payload(
    reports: list[dict[str, Any]],
    runtime_module_hashes: Mapping[str, str],
    expected_case_count: int,
    *,
    bindings: Mapping[str, Any] | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    completed = len(reports) == expected_case_count and reason_code is None
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "probe": PROTOCOL_ID,
        "status": "completed" if completed else "stopped",
        "reason_code": None if completed else reason_code or "CASE_STOPPED",
        **_receipt_identity(bindings),
        "runtime_module_sha256": dict(runtime_module_hashes),
        "cases": reports,
        "candidate_metrics_included": False,
    }


def _native_deadline_receipt(
    *,
    deadline: float,
    reports: list[dict[str, Any]],
    runtime_module_hashes: Mapping[str, str],
    expected_case_count: int,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if time.monotonic() < deadline:
        return None
    return _child_receipt_payload(
        reports,
        runtime_module_hashes,
        expected_case_count,
        bindings=bindings,
        reason_code="NATIVE_DEADLINE",
    )


def _write_report(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise CompatibilityError("MS5A probe report path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def execute_profile(
    *,
    runtime_root: Path,
    runtime_source: Path,
    market_data_root: Path,
    authority_path: Path,
    claim_root: Path,
    scratch_root: Path,
    report_path: Path,
    controller_root: Path,
) -> dict[str, Any]:
    _require_profile_execution_enabled()
    started = time.monotonic()
    memory_cap = require_hard_memory_cap()
    outer_watchdog = require_outer_watchdog()
    runtime_root = _safe_directory(runtime_root, must_exist=True)
    bindings, cases = _select(runtime_root)
    bindings = _bind_execution_identity(bindings, runtime_source, controller_root)
    runtime_source = _safe_directory(runtime_source, must_exist=True)
    authority = _read_json(authority_path)
    validate_authority(authority, bindings, cases)
    authority_id = authority["authority_id"]
    manifest_sha = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    try:
        if "strategy_factory.multi_strategy_owner" in sys.modules:
            raise ExternalComponentUnavailableError(
                "registered owner module was loaded before the protected child boundary"
            )
        _require_runtime_owner_diagnostics_source(runtime_source, bindings)
    except ExternalComponentUnavailableError as exc:
        child_failure = _setup_failure_payload(
            _ChildPhaseFailure("runtime_interface", exc), bindings, {}
        )
        receipt = _sanitized_child_receipt(
            json.dumps(child_failure, sort_keys=True, separators=(",", ":")), bindings
        )
        payload: dict[str, Any] = {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "probe": PROTOCOL_ID,
            "status": "setup_failure",
            "reason_code": child_failure["reason_code"],
            "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
            "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
            "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
            "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
            "controller_commit_sha": bindings["controller_commit_sha"],
            "authority_manifest_sha256": manifest_sha,
            "claim_consumed": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "child_hard_timeout_seconds": CHILD_HARD_SECONDS,
            "outer_hard_timeout_seconds": OUTER_HARD_SECONDS,
            "memory_limit_bytes": RSS_LIMIT_BYTES,
            "enforced_cgroup_memory_max_bytes": memory_cap["memory_max_bytes"],
            "enforced_cgroup_swap_max_bytes": memory_cap["memory_swap_max_bytes"],
            "enforced_outer_watchdog": outer_watchdog,
            "candidate_metrics_included": False,
            "cases": receipt["cases"],
            "runtime_module_sha256": receipt["runtime_module_sha256"],
            "setup_failure": receipt["setup_failure"],
        }
        _write_report(report_path.expanduser().absolute(), payload)
        return payload
    market_data_root = _safe_directory(market_data_root, must_exist=True)
    scratch_root = _prepare_scratch(scratch_root)
    if time.monotonic() - started >= OUTER_HARD_SECONDS:
        raise TimeoutError("MS5A probe outer deadline expired before launch")
    claim = _consume_claim(claim_root, authority_id, manifest_sha)
    child_args = (
        "--child",
        "--runtime-root",
        str(runtime_root),
        "--market-data-root",
        str(market_data_root),
        "--runtime-source",
        str(runtime_source),
        "--scratch-root",
        str(scratch_root),
        "--bindings-json",
        json.dumps(dict(bindings), sort_keys=True, separators=(",", ":")),
    )
    status, return_code, reason, stdout = launch_bounded_child(
        _child_command(
            runtime_source,
            controller_root,
            child_args,
            bindings.get("controller_module_sha256", {}),
            bindings.get("runtime_controller_api_sha256", {}),
        ),
        cwd=runtime_source,
        environment={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout_seconds=min(
            CHILD_HARD_SECONDS,
            max(1, int(OUTER_HARD_SECONDS - (time.monotonic() - started))),
        ),
    )
    bootstrap_diagnostics = _collect_bootstrap_diagnostics(scratch_root, runtime_source)
    receipt: dict[str, Any] = {"cases": [], "runtime_module_sha256": {}}
    if stdout:
        try:
            receipt = _sanitized_child_receipt(stdout, bindings)
        except CompatibilityError:
            if status == "completed":
                status = "invalid_receipt"
                reason = "CHILD_RECEIPT_INVALID"
    elapsed = round(time.monotonic() - started, 3)
    payload: dict[str, Any] = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "probe": PROTOCOL_ID,
        "status": "outer_timeout" if elapsed > OUTER_HARD_SECONDS else status,
        "reason_code": "OUTER_WALL_TIMEOUT" if elapsed > OUTER_HARD_SECONDS else reason or None,
        "return_code": return_code,
        "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
        "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
        "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
        "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
        "controller_commit_sha": bindings["controller_commit_sha"],
        "authority_manifest_sha256": manifest_sha,
        "claim_consumed": claim.exists(),
        "elapsed_seconds": elapsed,
        "child_hard_timeout_seconds": CHILD_HARD_SECONDS,
        "outer_hard_timeout_seconds": OUTER_HARD_SECONDS,
        "memory_limit_bytes": RSS_LIMIT_BYTES,
        "enforced_cgroup_memory_max_bytes": memory_cap["memory_max_bytes"],
        "enforced_cgroup_swap_max_bytes": memory_cap["memory_swap_max_bytes"],
        "enforced_outer_watchdog": outer_watchdog,
        "candidate_metrics_included": False,
        "bootstrap_diagnostics": bootstrap_diagnostics,
        "cases": receipt["cases"],
        "runtime_module_sha256": receipt["runtime_module_sha256"],
    }
    if "setup_failure" in receipt:
        payload["setup_failure"] = receipt["setup_failure"]
    _write_report(report_path.expanduser().absolute(), payload)
    return payload


def plan_only_report(
    runtime_root: Path, runtime_source: Path, controller_root: Path
) -> dict[str, Any]:
    bindings, cases = _select(runtime_root)
    bindings = _bind_execution_identity(bindings, runtime_source, controller_root)
    authority = authority_manifest(bindings, cases, "review-only-template")
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "probe": PROTOCOL_ID,
        "status": "plan_only_no_data_loaded_no_claim_consumed",
        "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
        "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
        "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
        "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
        "controller_commit_sha": bindings["controller_commit_sha"],
        "bindings": dict(bindings),
        "case_plan_hashes": [case.public() for case in cases],
        "execution_order": [
            {"family": case.family, "bars": bars, "stage_role": "optimization_000"}
            for bars in WINDOWS
            for case in cases
        ],
        "native_deadline_seconds_from_child_start": CHILD_NATIVE_DEADLINE_SECONDS,
        "child_hard_timeout_seconds": CHILD_HARD_SECONDS,
        "outer_hard_timeout_seconds": OUTER_HARD_SECONDS,
        "memory_limit_bytes": RSS_LIMIT_BYTES,
        "authority_template_sha256": hashlib.sha256(
            json.dumps(authority, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "candidate_metrics_included": False,
        "real_backtest_executed": False,
    }


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _prefix_job(job: Any, bars: int, output_directory: Path) -> Any:
    from modular_research_runner.models import TimeRange

    if job.period is None or job.stage_role != "optimization_000":
        raise CompatibilityError("MS5A profile job is outside first frozen SEARCH stage")
    end = job.period.start + timedelta(seconds=bars * 300)
    if end > job.period.end:
        raise CompatibilityError("MS5A profile prefix escapes frozen SEARCH period")
    return replace(
        job,
        run_id=f"{job.run_id}-ms5a-profile-{bars}",
        period=TimeRange(job.period.start, end),
        output_directory=output_directory,
    )


def _prefix_input_hash(market_data_root: Path, start: Any, end: Any, bars: int) -> str:
    _require_profile_execution_enabled()
    from strategy_factory.multi_strategy_owner import MS5A_DATASET_ID, _market_data_api

    resolve_klines, iter_klines, *_ = _market_data_api()
    selection = resolve_klines(
        market_data_root,
        MS5A_DATASET_ID,
        1,
        timeframe="5m",
        consumer="strategy_factory",
        deep=False,
    )
    digest = hashlib.sha256()
    count = 0
    for row in iter_klines(selection, start=start, end=end):
        values = (
            row.open_time.astimezone(UTC).isoformat(),
            str(row.open),
            str(row.high),
            str(row.low),
            str(row.close),
            str(row.volume),
        )
        digest.update(json.dumps(values, separators=(",", ":")).encode("utf-8") + b"\n")
        count += 1
    if count != bars:
        raise CompatibilityError("registered prefix has a different row count")
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    if root.exists():
        for item in sorted(root.rglob("*")):
            if item.is_symlink():
                raise CompatibilityError("Backtest output tree contains a symlink")
            if item.is_file():
                digest.update(item.relative_to(root).as_posix().encode() + b"\0")
                digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


def _child_run(
    runtime_source: Path,
    runtime_root: Path,
    market_data_root: Path,
    scratch_root: Path,
    expected_bindings: Mapping[str, str],
    diagnostics: _DiagnosticLedger | None = None,
    controller_root: Path | None = None,
) -> dict[str, Any]:
    _require_profile_execution_enabled()
    child_started = time.monotonic()
    native_deadline = child_started + CHILD_NATIVE_DEADLINE_SECONDS
    if diagnostics is not None:
        diagnostics.record("native_imports_started")
    lineage_bindings, cases = _select(runtime_root)
    bound_controller_root = controller_root or Path(__file__).resolve().parents[4]
    bindings = _bind_execution_identity(lineage_bindings, runtime_source, bound_controller_root)
    if dict(bindings) != dict(expected_bindings):
        raise CompatibilityError("child frozen/runtime bindings differ from parent selection")
    if diagnostics is not None:
        diagnostics.record("selection_completed")
    repaired_module_hashes = _attest_repaired_mdi_modules(runtime_source)
    if diagnostics is not None:
        diagnostics.record("runtime_attestation_completed")

    try:
        _verify_runtime_strategy_factory_package(runtime_source)
        _require_runtime_owner_diagnostics_source(runtime_source, expected_bindings)
        owner_module = _load_verified_owner_module(runtime_source, expected_bindings)
        create_owner_diagnostics, build_owner_state, owner_state_type = (
            _require_owner_diagnostic_support(owner_module)
        )
        from strategy_factory.multi_strategy_search_surface import (
            build_ms5a_search_surface,
        )

        _verify_runtime_module_origin(
            "strategy_factory.multi_strategy_search_surface", runtime_source
        )
        surface = build_ms5a_search_surface(source_commit_sha=SOURCE_SHA)
    except (ImportError, ExternalComponentUnavailableError) as exc:
        raise _ChildPhaseFailure("runtime_interface", exc, dict(repaired_module_hashes)) from exc

    from modular_backtest import run_backtest
    from modular_backtest.scalability import PerformanceConfig
    from modular_research_runner import ResearchPlanBuilder

    from strategy_factory.contract_codec import decode_strategy_factory_request
    from strategy_factory.multi_strategy_research_runtime import (
        MS5ABacktestConfigFactory,
        _OwnerFundingBacktestRunner,
    )
    from strategy_factory.run import StrategyFactoryRun

    if diagnostics is not None:
        diagnostics.record("native_imports_completed")
    request = decode_strategy_factory_request(_read_json(runtime_root / "frozen-request.json"))
    owner_diagnostics = _create_owner_diagnostics(
        create_owner_diagnostics,
        deadline_monotonic=native_deadline,
        record=diagnostics.record if diagnostics is not None else lambda *_args: None,
        runtime_module_hashes=repaired_module_hashes,
    )
    try:
        loaded_owner = _profile_owner_resolution(
            lambda: build_owner_state(
                market_data_root,
                created_at=request.created_at,
                diagnostics=owner_diagnostics,
            ),
            scratch_root / "owner-resolution.prof",
            diagnostics,
        )
    except _ChildPhaseFailure as exc:
        exc.runtime_module_hashes = dict(repaired_module_hashes)
        raise
    except ExternalComponentUnavailableError as exc:
        raise _ChildPhaseFailure("owner_resolution", exc, dict(repaired_module_hashes)) from exc
    if (
        loaded_owner.request.dataset.sha256 != request.dataset.sha256
        or loaded_owner.request.dataset.funding_sha256 != request.dataset.funding_sha256
        or loaded_owner.request.dataset.dataset_id != request.dataset.dataset_id
    ):
        raise CompatibilityError("registered owner differs from frozen input identity")
    owner = owner_state_type(
        request=request,
        dataset_file_size=loaded_owner.dataset_file_size,
        owner_row_count=loaded_owner.owner_row_count,
        funding_source=loaded_owner.funding_source,
    )
    runtime_module_hashes = _attest_runtime_modules(runtime_source, surface)
    scratch_root = _safe_directory(scratch_root, must_exist=True)
    metadata_root = _safe_directory(scratch_root / "metadata", must_exist=False)
    result_root = _safe_directory(scratch_root / "case-results", must_exist=False)
    profile_root = _safe_directory(scratch_root / "profiles", must_exist=False)
    run = StrategyFactoryRun(
        request=request, commit_sha=SOURCE_SHA, state_root=metadata_root, module_versions={}
    )
    if run.run_id != "sf-run-4a2799648bcd52ca8ad7e185":
        raise CompatibilityError("reconstructed Factory run differs from accepted lineage")
    config_factory = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=market_data_root,
        allowed_output_root=result_root,
    )
    if time.monotonic() >= native_deadline:
        raise TimeoutError("registered owner setup exhausted native deadline")
    funding_runner = _OwnerFundingBacktestRunner(
        run_backtest=run_backtest,
        performance_config=partial(
            PerformanceConfig,
            max_memory_mb=PROFILE_LIMIT_MB,
        ),
        funding_source=owner.funding_source,
        execution_window_seconds=max(1, int(native_deadline - time.monotonic())),
    )
    reports: list[dict[str, Any]] = []
    planned_jobs: list[tuple[FrozenCase, Any]] = []
    for case in cases:
        if diagnostics is not None:
            diagnostics.record("plan_build_started")
        spec = _native_specification(case.specification)
        plan = ResearchPlanBuilder(result_root / "plans").build(
            experiment_id=case.experiment_id, specification=spec
        )
        if diagnostics is not None:
            diagnostics.record("plan_build_returned")
        if not plan.jobs:
            raise CompatibilityError("frozen Research plan contains no SEARCH jobs")
        source_job = plan.jobs[0]
        if (
            source_job.stage_role != "optimization_000"
            or source_job.parameters.get("factory_run_id") != run.run_id
        ):
            raise CompatibilityError("native job differs from frozen family/seed lineage")
        planned_jobs.append((case, source_job))
    for bars in WINDOWS:
        for case, source_job in planned_jobs:
            deadline_receipt = _native_deadline_receipt(
                deadline=native_deadline,
                reports=reports,
                runtime_module_hashes=runtime_module_hashes,
                expected_case_count=len(cases) * len(WINDOWS),
                bindings=bindings,
            )
            if deadline_receipt is not None:
                return deadline_receipt
            if diagnostics is not None:
                diagnostics.record("case_started")
            key = f"{case.family}-{bars}"
            output = result_root / key
            output.mkdir(parents=False, exist_ok=False)
            job = _prefix_job(source_job, bars, output)
            input_hash = _prefix_input_hash(
                market_data_root, job.period.start, job.period.end, bars
            )
            profile_file = profile_root / f"{key}.prof"
            profiler = cProfile.Profile()
            started = time.monotonic()
            status = "failed"
            reason: str | None = None
            try:
                config = config_factory(job)
                profiler.runcall(
                    funding_runner.run_with_performance_mode,
                    config,
                    performance_mode="research_fast",
                )
                status = "completed"
            except Exception as exc:
                reason = type(exc).__name__.upper()
            finally:
                profiler.dump_stats(profile_file)
                output_hash = _tree_sha256(output)
                shutil.rmtree(output, ignore_errors=True)
            reports.append(
                {
                    "family": case.family,
                    "bars": bars,
                    "stage_role": "optimization_000",
                    "status": status,
                    "reason_code": reason,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "input_sha256": input_hash,
                    "result_tree_sha256": output_hash,
                    "profile_sha256": hashlib.sha256(profile_file.read_bytes()).hexdigest(),
                }
            )
            if diagnostics is not None:
                diagnostics.record("case_operation_returned")
            if status != "completed":
                break
        if reports and reports[-1]["status"] != "completed":
            break
    return _child_receipt_payload(
        reports,
        runtime_module_hashes,
        len(cases) * len(WINDOWS),
        bindings=bindings,
    )


def _child_main(argv: Sequence[str]) -> int:
    _require_profile_execution_enabled()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--market-data-root", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--bindings-json", required=True)
    args = parser.parse_args(argv)
    diagnostics: _DiagnosticLedger | None = None
    trace_stream: BinaryIO | None = None
    bindings: Mapping[str, Any] | None = None
    try:
        scratch_root = _safe_directory(args.scratch_root, must_exist=True)
        diagnostics = _DiagnosticLedger(scratch_root)
        diagnostics.record("child_started")
        trace_stream = _start_bootstrap_trace(scratch_root)
        parsed_bindings = strict_json_loads(args.bindings_json)
        if not isinstance(parsed_bindings, Mapping):
            raise CompatibilityError("child bindings are malformed")
        bindings = parsed_bindings
        _verify_runtime_checkout(args.runtime_source)
        controller_root = Path(__file__).resolve().parents[4]
        _verify_controller_checkout(
            controller_root,
            expected_sha=str(bindings.get("controller_commit_sha", "")),
        )
        diagnostics.record("runtime_checkout_verified")
        payload = _child_run(
            args.runtime_source,
            args.runtime_root,
            args.market_data_root,
            args.scratch_root,
            bindings,
            diagnostics,
            controller_root,
        )
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "completed" else 1
    except BaseException as exc:
        if isinstance(exc, _ChildPhaseFailure):
            payload = _setup_failure_payload(exc, bindings)
        else:
            payload = {"status": "failed", "reason_code": _safe_exception_class(exc).upper()}
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 1
    finally:
        try:
            _stop_bootstrap_trace(trace_stream)
        except (OSError, RuntimeError, ValueError):
            pass
        if diagnostics is not None:
            try:
                diagnostics.close()
            except (OSError, RuntimeError, ValueError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--market-data-root", type=Path)
    parser.add_argument("--authority-manifest", type=Path)
    parser.add_argument("--claim-root", type=Path)
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.plan_only:
            _require_profile_execution_enabled()
        controller_root = Path(__file__).resolve().parents[4]
        if args.plan_only:
            payload = plan_only_report(args.runtime_root, args.runtime_source, controller_root)
            if args.report is not None:
                _write_report(args.report.expanduser().absolute(), payload)
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return 0
        if any(
            item is None
            for item in (
                args.market_data_root,
                args.authority_manifest,
                args.claim_root,
                args.scratch_root,
                args.report,
            )
        ):
            parser.error(
                "execution requires market-data-root, authority-manifest, claim-root, scratch-root and report"
            )
        payload = execute_profile(
            runtime_root=args.runtime_root,
            runtime_source=args.runtime_source,
            market_data_root=args.market_data_root,
            authority_path=args.authority_manifest,
            claim_root=args.claim_root,
            scratch_root=args.scratch_root,
            report_path=args.report,
            controller_root=controller_root,
        )
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "completed" else 1
    except _ProfileExecutionStopped:
        print(json.dumps({"status": "blocked", "reason_code": _PROFILE_EXECUTION_STOPPED_REASON}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason_code": type(exc).__name__.upper()}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
