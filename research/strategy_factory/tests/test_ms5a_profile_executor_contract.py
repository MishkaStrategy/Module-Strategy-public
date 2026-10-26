from __future__ import annotations

import hashlib
import json
import signal
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from strategy_factory import ms5a_profile_executor as executor
from strategy_factory.canonical import canonical_sha256
from strategy_factory.errors import CompatibilityError, ExternalComponentUnavailableError
from strategy_factory.ms5a_budget_probe import (
    DIAGNOSTIC_REPAIR_ID,
    DIAGNOSTIC_RUNTIME_FILE_SHA256,
    DIAGNOSTIC_RUNTIME_SHA,
    EXPECTED_RUNTIME_SURFACE_FINGERPRINT,
    FAMILIES,
    FROZEN_LINEAGE_SHA,
    SOURCE_SHA,
    WINDOWS,
    FrozenCase,
    authority_manifest,
)

_DEFAULT_PROFILE_EXECUTION_GUARD = executor._require_profile_execution_enabled


@pytest.fixture(autouse=True)
def allow_synthetic_profile_contract_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permit legacy contract tests to exercise synthetic internals only."""
    monkeypatch.setattr(executor, "_require_profile_execution_enabled", lambda: None)


def _bindings() -> dict[str, Any]:
    return {
        "source_commit_sha": SOURCE_SHA,
        "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
        "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
        "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
        "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
        "controller_commit_sha": "d" * 40,
        "controller_module_sha256": {name: "e" * 64 for name in executor._CONTROLLER_MODULES},
        "runtime_controller_api_sha256": {
            name: "f" * 64 for name in executor._RUNTIME_CONTROLLER_API_MODULES
        },
        "request_fingerprint": "a" * 64,
        "dataset_fingerprint": "b" * 64,
        "dataset_sha256": "c" * 64,
        "runtime_surface_fingerprint": EXPECTED_RUNTIME_SURFACE_FINGERPRINT,
    }


def _cases() -> tuple[FrozenCase, ...]:
    return tuple(
        FrozenCase(
            family,
            canonical_sha256({"family": family, "plan": index}),
            {"research_id": f"research-{family}"},
            f"experiment-{index}",
            index < 3,
        )
        for index, family in enumerate(FAMILIES)
    )


def _module_hashes() -> dict[str, str]:
    return {
        "market_data_ingestion.integrity": DIAGNOSTIC_RUNTIME_FILE_SHA256[
            "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
        ],
        "market_data_ingestion.storage": DIAGNOSTIC_RUNTIME_FILE_SHA256[
            "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
        ],
    }


def _setup_failure_payload(exc: executor._ChildPhaseFailure) -> dict[str, Any]:
    return executor._setup_failure_payload(exc, _bindings(), _module_hashes())


def _compatible_owner_module() -> SimpleNamespace:
    def create_diagnostics(*, deadline_monotonic: float, record: Any) -> Any:
        return (deadline_monotonic, record)

    def build_owner(_root: Path, *, created_at: datetime, diagnostics: Any) -> Any:
        return (_root, created_at, diagnostics)

    def make_owner_state(
        *, request: Any, dataset_file_size: int, owner_row_count: int, funding_source: Any
    ) -> Any:
        return request, dataset_file_size, owner_row_count, funding_source

    return SimpleNamespace(
        OWNER_DIAGNOSTICS_INTERFACE_VERSION=executor.OWNER_DIAGNOSTICS_INTERFACE_VERSION,
        create_owner_resolution_diagnostics=create_diagnostics,
        build_ms5a_owner_state=build_owner,
        OwnerState=make_owner_state,
    )


def test_owner_diagnostic_interface_requires_version_factory_and_explicit_builder() -> None:
    compatible = _compatible_owner_module()
    assert executor._require_owner_diagnostic_support(compatible) == (
        compatible.create_owner_resolution_diagnostics,
        compatible.build_ms5a_owner_state,
        compatible.OwnerState,
    )

    old_runtime = SimpleNamespace(build_ms5a_owner_state=lambda *_args, **_kwargs: None)
    with pytest.raises(ExternalComponentUnavailableError):
        executor._require_owner_diagnostic_support(old_runtime)

    arbitrary_kwargs = _compatible_owner_module()
    arbitrary_kwargs.build_ms5a_owner_state = lambda *, created_at, **kwargs: (created_at, kwargs)
    with pytest.raises(ExternalComponentUnavailableError):
        executor._require_owner_diagnostic_support(arbitrary_kwargs)

    missing_factory = _compatible_owner_module()
    del missing_factory.create_owner_resolution_diagnostics
    with pytest.raises(ExternalComponentUnavailableError):
        executor._require_owner_diagnostic_support(missing_factory)


@pytest.mark.parametrize(
    "invalid_interface",
    [
        "factory_extra",
        "builder_extra",
        "state_extra",
        "async_factory",
        "async_builder",
        "generator_factory",
        "async_generator_factory",
        "generator_method",
        "async_generator_method",
    ],
)
def test_owner_diagnostic_interface_rejects_late_binding_and_async_contracts(
    invalid_interface: str,
) -> None:
    module = _compatible_owner_module()
    if invalid_interface == "factory_extra":
        module.create_owner_resolution_diagnostics = lambda *, deadline_monotonic, record, extra: (
            deadline_monotonic,
            record,
            extra,
        )
    elif invalid_interface == "builder_extra":
        module.build_ms5a_owner_state = lambda _root, *, created_at, diagnostics, extra: (
            created_at,
            diagnostics,
            extra,
        )
    elif invalid_interface == "state_extra":
        module.OwnerState = (
            lambda *, request, dataset_file_size, owner_row_count, funding_source, extra: (
                request,
                dataset_file_size,
                owner_row_count,
                funding_source,
                extra,
            )
        )
    elif invalid_interface == "async_factory":

        async def factory(*, deadline_monotonic: float, record: Any) -> Any:
            return deadline_monotonic, record

        module.create_owner_resolution_diagnostics = factory
    elif invalid_interface == "async_builder":

        async def builder(_root: Path, *, created_at: datetime, diagnostics: Any) -> Any:
            return _root, created_at, diagnostics

        module.build_ms5a_owner_state = builder
    elif invalid_interface == "generator_factory":

        def factory(*, deadline_monotonic: float, record: Any) -> Any:
            yield deadline_monotonic, record

        module.create_owner_resolution_diagnostics = factory
    elif invalid_interface == "async_generator_factory":

        async def factory(*, deadline_monotonic: float, record: Any) -> Any:
            yield deadline_monotonic, record

        module.create_owner_resolution_diagnostics = factory
    elif invalid_interface == "generator_method":

        class Factory:
            def __call__(self, *, deadline_monotonic: float, record: Any) -> Any:
                yield deadline_monotonic, record

        module.create_owner_resolution_diagnostics = Factory()
    else:

        class Factory:
            async def __call__(self, *, deadline_monotonic: float, record: Any) -> Any:
                yield deadline_monotonic, record

        module.create_owner_resolution_diagnostics = Factory()

    with pytest.raises(ExternalComponentUnavailableError):
        executor._require_owner_diagnostic_support(module)


def test_runtime_interface_failure_has_phase_specific_reason_code() -> None:
    payload = _setup_failure_payload(
        executor._ChildPhaseFailure(
            "runtime_interface", ExternalComponentUnavailableError("private detail")
        )
    )

    assert payload["reason_code"] == "OWNER_DIAGNOSTIC_INTERFACE_UNSUPPORTED"
    assert (
        executor._sanitized_child_receipt(json.dumps(payload), _bindings())["setup_failure"][
            "phase"
        ]
        == "runtime_interface"
    )

    payload["reason_code"] = "EXTERNALCOMPONENTUNAVAILABLEERROR"
    with pytest.raises(CompatibilityError):
        executor._sanitized_child_receipt(json.dumps(payload), _bindings())


@pytest.mark.parametrize("factory_mode", ["none", "raises", "missing-method", "missing-record"])
def test_diagnostics_factory_failures_are_structured_before_owner_call(factory_mode: str) -> None:
    owner_calls: list[str] = []

    def factory(**_kwargs: Any) -> Any:
        if factory_mode == "raises":
            raise RuntimeError("synthetic factory failure")
        if factory_mode == "none":
            return None
        protocol = SimpleNamespace(
            start=lambda _phase: None,
            finish=lambda _phase, *, counter_name=None, counter_value=None: None,
            record=lambda _phase, _counter_name, _counter_value: None,
            check_deadline=lambda: None,
        )
        if factory_mode == "missing-record":
            del protocol.record
        if factory_mode == "missing-method":
            del protocol.start
        return protocol

    with pytest.raises(executor._ChildPhaseFailure) as raised:
        diagnostics = executor._create_owner_diagnostics(
            factory,
            deadline_monotonic=1.0,
            record=lambda *_args: None,
            runtime_module_hashes=_module_hashes(),
        )
        del diagnostics
        owner_calls.append("builder")
    payload = _setup_failure_payload(raised.value)

    assert raised.value.phase == "runtime_interface"
    assert payload["setup_failure"]["error_class"] == "ExternalComponentUnavailableError"
    assert "synthetic factory failure" not in json.dumps(payload)
    assert owner_calls == []


def test_parent_static_negotiation_requires_complete_synchronous_call_contract(
    tmp_path: Path,
) -> None:
    source = tmp_path / "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py"
    source.parent.mkdir(parents=True)
    prefix = (
        "from dataclasses import dataclass\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass\n"
        "class OwnerState:\n"
        "    request: object\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n"
    )
    valid_calls = (
        "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
        "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n"
    )

    def check_source(source_text: str, *, accepted: bool) -> None:
        source.write_text(source_text, encoding="utf-8")
        expected_bindings = {
            "runtime_file_sha256": {
                executor._OWNER_DIAGNOSTICS_MODULE: hashlib.sha256(source.read_bytes()).hexdigest()
            }
        }
        if accepted:
            executor._require_runtime_owner_diagnostics_source(tmp_path, expected_bindings)
        else:
            with pytest.raises(ExternalComponentUnavailableError):
                executor._require_runtime_owner_diagnostics_source(tmp_path, expected_bindings)

    check_source(prefix + valid_calls, accepted=True)
    with pytest.raises(ExternalComponentUnavailableError):
        executor._require_runtime_owner_diagnostics_source(tmp_path, _bindings())

    invalid_calls = (
        "async def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
        "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
        "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
        "async def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
        "def create_owner_resolution_diagnostics(*, deadline_monotonic, record, extra):\n    pass\n"
        "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
        "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
        "def build_ms5a_owner_state(root, *, created_at, diagnostics, extra):\n    pass\n",
    )
    for calls in invalid_calls:
        check_source(prefix + calls, accepted=False)
    check_source(
        prefix
        + "def create_owner_resolution_diagnostics(*, deadline_monotonic, record, extra=1):\n    pass\n"
        + "def build_ms5a_owner_state(root, *, created_at, diagnostics, extra=1):\n    pass\n",
        accepted=True,
    )
    check_source(
        prefix + "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n"
        "    def helper():\n        yield 1\n"
        "        OwnerState = object\n"
        "    return helper\n"
        + "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
        accepted=True,
    )
    check_source(
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
        "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
        accepted=False,
    )
    check_source(
        "from dataclasses import dataclass, field\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass(init=False)\n"
        "class OwnerState:\n"
        "    request: object\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n" + valid_calls,
        accepted=False,
    )
    check_source(
        "from dataclasses import dataclass\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass(init=False)\n"
        "class OwnerState:\n"
        "    request: object\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n"
        "    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass\n"
        + valid_calls,
        accepted=True,
    )
    for decorator in ("@dataclass(init=0)", "@dataclass(init=unknown)"):
        check_source(
            "from dataclasses import dataclass, field\n"
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
            f"{decorator}\n"
            "class OwnerState:\n"
            "    request: object\n"
            "    dataset_file_size: int\n"
            "    owner_row_count: int\n"
            "    funding_source: object\n" + valid_calls,
            accepted=False,
        )
    for field_init in ("0", "unknown"):
        check_source(
            "from dataclasses import dataclass, field\n"
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
            "@dataclass\n"
            "class OwnerState:\n"
            f"    request: object = field(init={field_init})\n"
            "    dataset_file_size: int\n"
            "    owner_row_count: int\n"
            "    funding_source: object\n" + valid_calls,
            accepted=False,
        )
    check_source(
        "from dataclasses import dataclass, field\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass\n"
        "class OwnerState:\n"
        "    request: object\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n"
        "    extra: object = field()\n" + valid_calls,
        accepted=False,
    )
    for extra_field in (
        "extra: object = field(default=None)",
        "extra: object = field(default_factory=list)",
        "extra: object = field(default=None, metadata={})",
    ):
        check_source(
            "from dataclasses import dataclass, field\n"
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
            "@dataclass\n"
            "class OwnerState:\n"
            "    request: object\n"
            "    dataset_file_size: int\n"
            "    owner_row_count: int\n"
            "    funding_source: object\n"
            f"    {extra_field}\n" + valid_calls,
            accepted=True,
        )
    check_source(
        "from dataclasses import dataclass\n"
        "from typing import Annotated, ClassVar\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass\n"
        "class OwnerState:\n"
        "    request: Annotated[object, ClassVar]\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n" + valid_calls,
        accepted=True,
    )
    check_source(
        "from dataclasses import dataclass, field\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass\n"
        "class OwnerState:\n"
        "    request: object = field(init=False)\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n" + valid_calls,
        accepted=False,
    )


@pytest.mark.parametrize(
    "owner_state_declaration",
    [
        "@dataclass(init=0)",
        "@dataclass(init=unknown)",
        "@dataclass(**kwargs)",
        "@dataclass(unknown=True)",
        "@dataclass(init=True, init=False)",
        "@dataclass(order=True, eq=False)\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "@dataclass(weakref_slot=True)\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "@dataclass(slots=True)\nclass OwnerState:\n    __slots__ = ()\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "@dataclass(slots=True)\nclass OwnerState:\n    __slots__ = ()\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass",
        "@dataclass(frozen=True)\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    def __setattr__(self, name, value): pass\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass",
        "@dataclass(order=True)\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    def __lt__(self, other): return False\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass",
        "@dataclass(unsafe_hash=True)\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    def __hash__(self): return 1\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass",
        "from typing import ClassVar as CV\n@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    extra: CV[object] = field(default_factory=list)",
        "from dataclasses import InitVar\n@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    extra: InitVar[object] = field(default_factory=list)",
        "from dataclasses import InitVar\n@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    extra: InitVar = field(default_factory=list)",
        "@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    extra: object = None\n    del extra",
        "@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    funding_source = field(init=False)",
        '@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    __annotations__["extra"] = object',
        '@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\nOwnerState.__annotations__["extra"] = object',
        "@dataclass\nclass OwnerState:\n    funding_source = field(init=False)\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    extra: object = field(metadata=0)\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass",
        "@dataclass\nclass OwnerState:\n    request: object = field(init=0)",
        "@dataclass\nclass OwnerState:\n    request: object = field(init=unknown)",
        "@dataclass\nclass OwnerState:\n    request: object = field(**kwargs)",
        "@dataclass\nclass OwnerState:\n    request: object = field(None)",
        "@dataclass\nclass OwnerState:\n    request: object = field(unknown=True)",
        "@dataclass\nclass OwnerState:\n    request: object = field(init=True, init=False)",
        "@dataclass\nclass OwnerState:\n    request: object = field(default=None, default_factory=list)",
        "@dataclass\nclass OwnerState:\n    extra: object = field()",
        "@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object = []",
        "@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object = field(default=[])",
        "from typing import ClassVar as CV\n@dataclass\nclass OwnerState:\n    request: CV[object]\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "from dataclasses import MISSING\n@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object\n    extra: object = field(default=MISSING)",
        "@dataclass\nclass OwnerState:\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass\n    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source, extra): pass",
        "@dataclass\nclass OwnerState:\n    def __new__(cls, *, token): return super().__new__(cls)\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "@dataclass\nclass OwnerState:\n    __signature__ = object()\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
        "@wrap\n@dataclass\nclass OwnerState:\n    request: object\n    dataset_file_size: int\n    owner_row_count: int\n    funding_source: object",
    ],
    ids=[
        "dataclass-init-zero",
        "dataclass-init-unknown",
        "dataclass-star-kwargs",
        "dataclass-unknown-keyword",
        "dataclass-duplicate-init",
        "dataclass-order-without-eq",
        "dataclass-weakref-without-slots",
        "dataclass-slots-with-explicit-slots",
        "dataclass-custom-init-slots-conflict",
        "dataclass-custom-init-frozen-setattr-conflict",
        "dataclass-custom-init-order-comparison-conflict",
        "dataclass-custom-init-unsafe-hash-conflict",
        "classvar-field-default",
        "initvar-default-factory",
        "bare-initvar-default-factory",
        "deleted-optional-default",
        "reassigned-required-field-after-annotation",
        "class-mutates-annotations",
        "module-mutates-owner-annotations",
        "assigned-field-before-annotation",
        "custom-init-invalid-field-metadata",
        "field-init-zero",
        "field-init-unknown",
        "field-star-kwargs",
        "field-positional-argument",
        "field-unknown-keyword",
        "field-duplicate-init",
        "field-duplicate-default",
        "required-field-extra",
        "mutable-literal-default",
        "mutable-field-default",
        "classvar-alias",
        "missing-sentinel-default",
        "duplicate-init",
        "custom-new",
        "custom-signature",
        "custom-class-decorator",
    ],
)
def test_parent_preflight_rejects_unsafe_dataclass_owner_state_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_state_declaration: str,
) -> None:
    paths = _execution_paths(tmp_path)
    require_interface = executor._require_runtime_owner_diagnostics_source
    _cases, launches = _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    monkeypatch.setattr(executor, "_require_runtime_owner_diagnostics_source", require_interface)
    owner_path = paths["runtime_source"] / executor._OWNER_DIAGNOSTICS_MODULE
    owner_path.parent.mkdir(parents=True)
    if "class OwnerState:" in owner_state_declaration:
        class_declaration = owner_state_declaration
    else:
        class_declaration = (
            owner_state_declaration + "\nclass OwnerState:\n"
            "    request: object\n"
            "    dataset_file_size: int\n"
            "    owner_row_count: int\n"
            "    funding_source: object"
        )
    owner_path.write_text(
        "from dataclasses import dataclass, field\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        + class_declaration
        + "\ndef create_owner_resolution_diagnostics(*, deadline_monotonic, record): pass\n"
        "def build_ms5a_owner_state(root, *, created_at, diagnostics): pass\n",
        encoding="utf-8",
    )

    report = executor.execute_profile(**paths)

    assert report["status"] == "setup_failure"
    assert report["reason_code"] == "OWNER_DIAGNOSTIC_INTERFACE_UNSUPPORTED"
    assert report["claim_consumed"] is False
    assert not (paths["claim_root"] / "consumed.json").exists()
    assert launches == []


@pytest.mark.parametrize(
    "mutation",
    [
        "decorated_factory",
        "decorated_builder",
        "reassigned_factory",
        "reassigned_builder",
        "reassigned_dataclass_helper",
        "reassigned_field_helper",
        "reassigned_owner_signature",
        "reassigned_owner_new",
        "duplicate_factory",
        "duplicate_builder",
    ],
)
def test_parent_preflight_rejects_decorated_or_rebound_boundary_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    paths = _execution_paths(tmp_path)
    require_interface = executor._require_runtime_owner_diagnostics_source
    _cases, launches = _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    monkeypatch.setattr(executor, "_require_runtime_owner_diagnostics_source", require_interface)
    factory = "def create_owner_resolution_diagnostics(*, deadline_monotonic, record): pass\n"
    builder = "def build_ms5a_owner_state(root, *, created_at, diagnostics): pass\n"
    suffix = ""
    if mutation == "decorated_factory":
        factory = "@wrapper\n" + factory
    elif mutation == "decorated_builder":
        builder = "@wrapper\n" + builder
    elif mutation == "reassigned_factory":
        suffix = "create_owner_resolution_diagnostics = None\n"
    elif mutation == "reassigned_builder":
        suffix = "build_ms5a_owner_state = None\n"
    elif mutation == "reassigned_dataclass_helper":
        suffix = "dataclass = lambda cls: cls\n"
    elif mutation == "reassigned_field_helper":
        suffix = "field = lambda **kwargs: kwargs\n"
    elif mutation == "reassigned_owner_signature":
        suffix = "OwnerState.__signature__ = object()\n"
    elif mutation == "reassigned_owner_new":
        suffix = "OwnerState.__new__ = lambda cls, *, token: object.__new__(cls)\n"
    elif mutation == "duplicate_factory":
        suffix = factory
    elif mutation == "duplicate_builder":
        suffix = builder
    owner_path = paths["runtime_source"] / executor._OWNER_DIAGNOSTICS_MODULE
    owner_path.parent.mkdir(parents=True)
    owner_fields = (
        "    request: object\n"
        "    dataset_file_size: int\n"
        "    owner_row_count: int\n"
        "    funding_source: object\n"
    )
    if mutation == "reassigned_field_helper":
        owner_fields += "    extra: object = field(default=None)\n"
    owner_path.write_text(
        "from dataclasses import dataclass, field\n"
        "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        "@dataclass\n"
        "class OwnerState:\n" + owner_fields + factory + builder + suffix,
        encoding="utf-8",
    )

    report = executor.execute_profile(**paths)

    assert report["status"] == "setup_failure"
    assert report["reason_code"] == "OWNER_DIAGNOSTIC_INTERFACE_UNSUPPORTED"
    assert report["claim_consumed"] is False
    assert not (paths["claim_root"] / "consumed.json").exists()
    assert launches == []


def test_imported_controller_fallback_is_rejected_by_bound_module_identity(
    tmp_path: Path,
) -> None:
    runtime_source = tmp_path / "runtime"
    runtime_source.mkdir()
    fallback = tmp_path / "controller" / "multi_strategy_owner.py"
    fallback.parent.mkdir()
    fallback.write_text("# controller fallback\n", encoding="utf-8")
    bindings = {
        "runtime_file_sha256": {
            executor._OWNER_DIAGNOSTICS_MODULE: hashlib.sha256(fallback.read_bytes()).hexdigest()
        }
    }

    with pytest.raises(ExternalComponentUnavailableError):
        executor._verify_imported_owner_module(
            SimpleNamespace(__file__=str(fallback)), runtime_source, bindings
        )


def test_controller_fallback_origin_is_rejected_before_owner_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    runtime_source = tmp_path / "runtime"
    owner_path = runtime_source / executor._OWNER_DIAGNOSTICS_MODULE
    owner_path.parent.mkdir(parents=True)
    owner_bytes = (
        b"OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        b"class OwnerState:\n"
        b"    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass\n"
        b"def create_owner_resolution_diagnostics(*, deadline_monotonic, record): pass\n"
        b"def build_ms5a_owner_state(root, *, created_at, diagnostics): pass\n"
        b"ORIGIN = 'verified-source'\n"
    )
    owner_path.write_bytes(owner_bytes)
    stale_bytecode = owner_path.parent / "__pycache__" / "multi_strategy_owner.cpython-314.pyc"
    stale_bytecode.parent.mkdir()
    stale_bytecode.write_bytes(b"synthetic stale bytecode must never be loaded")
    bindings = {
        "runtime_file_sha256": {
            executor._OWNER_DIAGNOSTICS_MODULE: hashlib.sha256(owner_bytes).hexdigest()
        }
    }
    monkeypatch.delitem(sys.modules, "strategy_factory.multi_strategy_owner", raising=False)
    monkeypatch.setitem(sys.modules, "strategy_factory", sys.modules["strategy_factory"])
    monkeypatch.setattr(executor, "_verify_runtime_strategy_factory_package", lambda _root: None)

    def forbidden_import(_name: str) -> Any:
        pytest.fail("owner must execute from the verified source snapshot, not an import loader")

    monkeypatch.setattr(executor.importlib, "import_module", forbidden_import)
    monkeypatch.setattr(executor.importlib, "find_spec", forbidden_import, raising=False)

    original_sha256 = hashlib.sha256

    def change_source_after_verification(data: bytes = b"") -> Any:
        result = original_sha256(data)
        if data == owner_bytes:
            owner_path.write_bytes(b"raise RuntimeError('stale loader executed')\n")
        return result

    monkeypatch.setattr(executor.hashlib, "sha256", change_source_after_verification)
    module = executor._load_verified_owner_module(runtime_source, bindings)

    assert module.ORIGIN == "verified-source"
    assert module.__file__ == str(owner_path.resolve())
    assert module.__spec__.origin == str(owner_path.resolve())
    assert owner_path.read_bytes().startswith(b"raise RuntimeError")
    assert sys.modules["strategy_factory.multi_strategy_owner"] is module
    sys.modules.pop("strategy_factory.multi_strategy_owner", None)
    delattr(sys.modules["strategy_factory"], "multi_strategy_owner")


def test_verified_owner_is_available_to_real_relative_fromlist_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    runtime_source = tmp_path / "runtime"
    owner_path = runtime_source / executor._OWNER_DIAGNOSTICS_MODULE
    owner_path.parent.mkdir(parents=True)
    owner_bytes = (
        b"OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        b"MS5A_ALLOWED_FAMILIES = frozenset({'synthetic'})\n"
        b"class OwnerState:\n"
        b"    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass\n"
        b"def create_owner_resolution_diagnostics(*, deadline_monotonic, record): pass\n"
        b"def build_ms5a_owner_state(root, *, created_at, diagnostics): pass\n"
    )
    owner_path.write_bytes(owner_bytes)
    package = sys.modules["strategy_factory"]
    owner_name = "strategy_factory.multi_strategy_owner"
    monkeypatch.delitem(sys.modules, owner_name, raising=False)
    monkeypatch.delattr(package, "multi_strategy_owner", raising=False)
    monkeypatch.delitem(sys.modules, "strategy_factory.synthetic_surface", raising=False)
    monkeypatch.delattr(package, "synthetic_surface", raising=False)
    monkeypatch.setattr(executor, "_verify_runtime_strategy_factory_package", lambda _root: None)
    bindings = {
        "runtime_file_sha256": {
            executor._OWNER_DIAGNOSTICS_MODULE: hashlib.sha256(owner_bytes).hexdigest()
        }
    }

    owner = executor._load_verified_owner_module(runtime_source, bindings)
    synthetic_surface = types.ModuleType("strategy_factory.synthetic_surface")
    synthetic_surface.__package__ = "strategy_factory"
    exec("from .multi_strategy_owner import MS5A_ALLOWED_FAMILIES", synthetic_surface.__dict__)

    assert package.multi_strategy_owner is owner
    assert sys.modules[owner_name] is owner
    assert synthetic_surface.MS5A_ALLOWED_FAMILIES is owner.MS5A_ALLOWED_FAMILIES
    sys.modules.pop(owner_name, None)
    delattr(package, "multi_strategy_owner")


def test_verified_owner_exec_failure_leaves_package_without_owner_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    runtime_source = tmp_path / "runtime"
    owner_path = runtime_source / executor._OWNER_DIAGNOSTICS_MODULE
    owner_path.parent.mkdir(parents=True)
    source = (
        b"OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        b"class OwnerState:\n"
        b"    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass\n"
        b"def create_owner_resolution_diagnostics(*, deadline_monotonic, record): pass\n"
        b"def build_ms5a_owner_state(root, *, created_at, diagnostics): pass\n"
        b"raise RuntimeError('synthetic owner module failure')\n"
    )
    owner_path.write_bytes(source)
    package = sys.modules["strategy_factory"]
    module_name = "strategy_factory.multi_strategy_owner"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.delattr(package, "multi_strategy_owner", raising=False)
    monkeypatch.setattr(executor, "_verify_runtime_strategy_factory_package", lambda _root: None)
    bindings = {
        "runtime_file_sha256": {
            executor._OWNER_DIAGNOSTICS_MODULE: hashlib.sha256(source).hexdigest()
        }
    }

    with pytest.raises(RuntimeError, match="synthetic owner module failure"):
        executor._load_verified_owner_module(runtime_source, bindings)

    assert module_name not in sys.modules
    assert not hasattr(package, "multi_strategy_owner")


def test_verified_owner_exec_failure_removes_only_its_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    runtime_source = tmp_path / "runtime"
    owner_path = runtime_source / executor._OWNER_DIAGNOSTICS_MODULE
    owner_path.parent.mkdir(parents=True)
    source = (
        b"raise RuntimeError('synthetic owner module failure')\n"
        b"OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
        b"class OwnerState:\n"
        b"    def __init__(self, *, request, dataset_file_size, owner_row_count, funding_source): pass\n"
        b"def create_owner_resolution_diagnostics(*, deadline_monotonic, record): pass\n"
        b"def build_ms5a_owner_state(root, *, created_at, diagnostics): pass\n"
    )
    owner_path.write_bytes(source)
    module_name = "strategy_factory.multi_strategy_owner"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(executor, "_verify_runtime_strategy_factory_package", lambda _root: None)
    bindings = {
        "runtime_file_sha256": {
            executor._OWNER_DIAGNOSTICS_MODULE: hashlib.sha256(source).hexdigest()
        }
    }

    with pytest.raises(RuntimeError, match="synthetic owner module failure"):
        executor._load_verified_owner_module(runtime_source, bindings)

    assert module_name not in sys.modules


def _child_receipt(*, failed: bool = False) -> str:
    cases = []
    for bars in WINDOWS:
        for family in FAMILIES:
            status = "failed" if failed and not cases else "completed"
            cases.append(
                {
                    "family": family,
                    "bars": bars,
                    "stage_role": "optimization_000",
                    "status": status,
                    "reason_code": "SYNTHETIC_FAILURE" if status == "failed" else None,
                    "duration_seconds": 0.25,
                    "input_sha256": "1" * 64,
                    "result_tree_sha256": "2" * 64,
                    "profile_sha256": "3" * 64,
                }
            )
            if failed and status == "failed":
                break
        if failed and cases[-1]["status"] == "failed":
            break
    return json.dumps(
        {
            "schema_version": 2,
            "probe": "ms5a-bounded-profile-v2-repaired-runtime",
            "frozen_lineage_source_commit_sha": FROZEN_LINEAGE_SHA,
            "runtime_source_commit_sha": DIAGNOSTIC_RUNTIME_SHA,
            "runtime_repair_id": DIAGNOSTIC_REPAIR_ID,
            "runtime_file_sha256": dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
            "controller_commit_sha": "d" * 40,
            "status": "stopped" if failed else "completed",
            "reason_code": "SYNTHETIC_FAILURE" if failed else None,
            "runtime_module_sha256": {
                "modular_backtest.runner": "4" * 64,
                "market_data_ingestion.integrity": DIAGNOSTIC_RUNTIME_FILE_SHA256[
                    "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
                ],
                "market_data_ingestion.storage": DIAGNOSTIC_RUNTIME_FILE_SHA256[
                    "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
                ],
            },
            "cases": cases,
            "candidate_metrics_included": False,
        }
    )


def _execution_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "runtime_root": tmp_path / "frozen-runtime",
        "runtime_source": tmp_path / "accepted-source",
        "market_data_root": tmp_path / "registered-data",
        "claim_root": tmp_path / "claim",
        "scratch_root": tmp_path / "scratch",
        "report_path": tmp_path / "report.json",
        "controller_root": tmp_path / "controller",
        "authority_path": tmp_path / "authority.json",
    }
    for name in (
        "runtime_root",
        "runtime_source",
        "market_data_root",
        "claim_root",
        "controller_root",
    ):
        paths[name].mkdir()
    return paths


def _install_parent_stubs(
    monkeypatch: pytest.MonkeyPatch,
    paths: dict[str, Path],
    *,
    child_stdout: str,
) -> tuple[tuple[FrozenCase, ...], list[tuple[Any, ...]]]:
    import sys

    monkeypatch.delitem(sys.modules, "strategy_factory.multi_strategy_owner", raising=False)
    cases = _cases()
    bindings = _bindings()
    authority = authority_manifest(bindings, cases, "synthetic-owner-grant")
    paths["authority_path"].write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setattr(executor, "_verify_runtime_checkout", lambda path: path)
    monkeypatch.setattr(executor, "_select", lambda _root: (bindings, cases))
    monkeypatch.setattr(executor, "_bind_execution_identity", lambda value, *_args: dict(value))
    monkeypatch.setattr(executor, "_require_runtime_owner_diagnostics_source", lambda *_args: None)
    monkeypatch.setattr(executor, "_git_head", lambda _root: "d" * 40)
    monkeypatch.setattr(
        executor,
        "require_hard_memory_cap",
        lambda: {"memory_max_bytes": executor.RSS_LIMIT_BYTES, "memory_swap_max_bytes": 0},
    )
    monkeypatch.setattr(
        executor,
        "require_outer_watchdog",
        lambda: {
            "pid1_executable": "timeout",
            "outer_timeout_seconds": executor.OUTER_HARD_SECONDS,
            "outer_signal": "KILL",
        },
    )
    calls: list[tuple[Any, ...]] = []

    def launch(command: Any, **kwargs: Any) -> tuple[str, int, str, str]:
        calls.append((tuple(command), kwargs))
        return "completed", 0, "", child_stdout

    monkeypatch.setattr(executor, "launch_bounded_child", launch)
    return cases, calls


def test_execute_profile_rejects_old_owner_interface_before_claim_or_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    require_interface = executor._require_runtime_owner_diagnostics_source
    _cases_for_refusal, launches = _install_parent_stubs(
        monkeypatch, paths, child_stdout=_child_receipt()
    )
    monkeypatch.setattr(executor, "_require_runtime_owner_diagnostics_source", require_interface)
    owner_path = (
        paths["runtime_source"]
        / "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py"
    )
    owner_path.parent.mkdir(parents=True)
    owner_path.write_text(
        "def build_ms5a_owner_state(root, *, created_at):\n    return root, created_at\n",
        encoding="utf-8",
    )

    report = executor.execute_profile(**paths)

    assert report["status"] == "setup_failure"
    assert report["reason_code"] == "OWNER_DIAGNOSTIC_INTERFACE_UNSUPPORTED"
    assert report["setup_failure"] == {
        "phase": "runtime_interface",
        "dataset_kind": None,
        "error_class": "ExternalComponentUnavailableError",
        "cause_classes": [],
    }
    assert report["cases"] == []
    assert report["runtime_module_sha256"] == {}
    assert (
        report["authority_manifest_sha256"]
        == hashlib.sha256(paths["authority_path"].read_bytes()).hexdigest()
    )
    assert report["claim_consumed"] is False
    assert not (paths["claim_root"] / "consumed.json").exists()
    assert launches == []


def test_execute_profile_normalizes_owner_path_escape_before_claim_or_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    require_interface = executor._require_runtime_owner_diagnostics_source
    _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    monkeypatch.setattr(executor, "_require_runtime_owner_diagnostics_source", require_interface)
    owner_link = paths["runtime_source"] / executor._OWNER_DIAGNOSTICS_MODULE
    owner_link.parent.mkdir(parents=True)
    external_owner = tmp_path / "outside-owner.py"
    external_owner.write_text("# synthetic external source\n", encoding="utf-8")
    owner_link.symlink_to(external_owner)

    report = executor.execute_profile(**paths)

    assert report["status"] == "setup_failure"
    assert report["setup_failure"]["phase"] == "runtime_interface"
    assert report["setup_failure"]["error_class"] == "ExternalComponentUnavailableError"
    assert report["claim_consumed"] is False
    assert not (paths["claim_root"] / "consumed.json").exists()


def test_execute_profile_rejects_preloaded_owner_before_claim_or_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    paths = _execution_paths(tmp_path)
    _cases, launches = _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    monkeypatch.setitem(
        sys.modules,
        "strategy_factory.multi_strategy_owner",
        SimpleNamespace(__file__="/synthetic/preloaded-owner.py"),
    )

    report = executor.execute_profile(**paths)

    assert report["status"] == "setup_failure"
    assert report["setup_failure"]["phase"] == "runtime_interface"
    assert report["claim_consumed"] is False
    assert not (paths["claim_root"] / "consumed.json").exists()
    assert launches == []


def test_execute_profile_validates_grant_consumes_once_and_publishes_only_sanitized_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    original_runtime = {
        p.relative_to(paths["runtime_root"]): p.read_bytes()
        for p in paths["runtime_root"].rglob("*")
        if p.is_file()
    }
    original_data = {
        p.relative_to(paths["market_data_root"]): p.read_bytes()
        for p in paths["market_data_root"].rglob("*")
        if p.is_file()
    }
    cases, calls = _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())

    payload = executor.execute_profile(**paths)

    assert payload["status"] == "completed"
    assert payload["claim_consumed"] is True
    assert (
        payload["authority_manifest_sha256"]
        == hashlib.sha256(paths["authority_path"].read_bytes()).hexdigest()
    )
    assert len(payload["cases"]) == len(cases) * len(WINDOWS)
    assert payload["candidate_metrics_included"] is False
    assert payload["enforced_cgroup_memory_max_bytes"] == executor.RSS_LIMIT_BYTES
    assert payload["enforced_cgroup_swap_max_bytes"] == 0
    assert payload["enforced_outer_watchdog"] == {
        "pid1_executable": "timeout",
        "outer_timeout_seconds": executor.OUTER_HARD_SECONDS,
        "outer_signal": "KILL",
    }
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == paths["runtime_source"]
    assert calls[0][1]["timeout_seconds"] == executor.CHILD_HARD_SECONDS
    assert json.loads(paths["report_path"].read_text()) == payload
    assert {
        p.relative_to(paths["runtime_root"]): p.read_bytes()
        for p in paths["runtime_root"].rglob("*")
        if p.is_file()
    } == original_runtime
    assert {
        p.relative_to(paths["market_data_root"]): p.read_bytes()
        for p in paths["market_data_root"].rglob("*")
        if p.is_file()
    } == original_data


def test_execute_profile_rejects_changed_grant_before_consumption_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    _cases_for_refusal, calls = _install_parent_stubs(
        monkeypatch, paths, child_stdout=_child_receipt()
    )
    manifest = json.loads(paths["authority_path"].read_text())
    manifest["window_bars"] = [1_000, 2_000]
    paths["authority_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CompatibilityError, match="authority"):
        executor.execute_profile(**paths)

    assert not (paths["claim_root"] / "consumed.json").exists()
    assert not calls
    assert not paths["report_path"].exists()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda manifest: manifest.update(schema_version=1), id="old-contract"),
        pytest.param(
            lambda manifest: manifest.update(runtime_source_commit_sha="e" * 40),
            id="runtime-source-drift",
        ),
        pytest.param(
            lambda manifest: manifest["runtime_file_sha256"].update(
                {next(iter(manifest["runtime_file_sha256"])): "f" * 64}
            ),
            id="runtime-helper-digest-drift",
        ),
        pytest.param(
            lambda manifest: manifest.update(controller_commit_sha="e" * 40),
            id="controller-source-drift",
        ),
    ],
)
def test_execute_profile_rejects_runtime_or_protocol_binding_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    paths = _execution_paths(tmp_path)
    _cases_for_refusal, calls = _install_parent_stubs(
        monkeypatch, paths, child_stdout=_child_receipt()
    )
    manifest = json.loads(paths["authority_path"].read_text())
    mutate(manifest)
    paths["authority_path"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CompatibilityError, match="authority"):
        executor.execute_profile(**paths)

    assert not (paths["claim_root"] / "consumed.json").exists()
    assert not calls


@pytest.mark.parametrize(
    ("status", "return_code", "reason", "stdout", "expected_status"),
    [
        ("child_failed", 1, "CALLBACK_FAILED", _child_receipt(failed=True), "child_failed"),
        ("memory_limit", -9, "CHILD_RSS_LIMIT", "", "memory_limit"),
        ("completed", 0, "", "not a receipt", "invalid_receipt"),
    ],
)
def test_execute_profile_records_child_failure_without_expanding_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    return_code: int,
    reason: str,
    stdout: str,
    expected_status: str,
) -> None:
    paths = _execution_paths(tmp_path)
    _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    monkeypatch.setattr(
        executor,
        "launch_bounded_child",
        lambda *_args, **_kwargs: (status, return_code, reason, stdout),
    )

    payload = executor.execute_profile(**paths)

    assert payload["status"] == expected_status
    assert payload["candidate_metrics_included"] is False
    assert json.loads(paths["report_path"].read_text()) == payload


@pytest.mark.parametrize(
    ("failed", "old_runtime", "owner_path_escape"),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
def test_child_pipeline_uses_accepted_owner_and_scratch_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    old_runtime: bool,
    owner_path_escape: bool,
) -> None:
    import modular_backtest
    import modular_backtest.scalability
    import modular_research_runner
    import modular_research_runner.engine
    import modular_research_runner.models

    import strategy_factory.contract_codec
    import strategy_factory.multi_strategy_owner
    import strategy_factory.multi_strategy_research_runtime
    import strategy_factory.multi_strategy_search_surface
    import strategy_factory.run

    runtime_root = tmp_path / "frozen-runtime"
    runtime_root.mkdir()
    (runtime_root / "frozen-request.json").write_text("{}", encoding="utf-8")
    runtime_before = {
        path.relative_to(runtime_root): path.read_bytes()
        for path in runtime_root.rglob("*")
        if path.is_file()
    }
    runtime_source = tmp_path / "accepted-source"
    runtime_source.mkdir()
    owner_source = (
        runtime_source / "research/strategy_factory/src/strategy_factory/multi_strategy_owner.py"
    )
    owner_source.parent.mkdir(parents=True)
    if owner_path_escape:
        external_owner = tmp_path / "outside-owner.py"
        external_owner.write_text(
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class OwnerState:\n"
            "    request: object\n"
            "    dataset_file_size: int\n"
            "    owner_row_count: int\n"
            "    funding_source: object\n"
            "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
            "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
            encoding="utf-8",
        )
        owner_source.symlink_to(external_owner)
        owner_bytes_for_binding = external_owner.read_bytes()
    elif old_runtime:
        owner_source.write_text(
            "def build_ms5a_owner_state(root, *, created_at):\n    pass\n",
            encoding="utf-8",
        )
        owner_bytes_for_binding = owner_source.read_bytes()
    else:
        owner_source.write_text(
            "from dataclasses import dataclass\n"
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION = 1\n"
            "@dataclass\n"
            "class OwnerState:\n"
            "    request: object\n"
            "    dataset_file_size: int\n"
            "    owner_row_count: int\n"
            "    funding_source: object\n"
            "def create_owner_resolution_diagnostics(*, deadline_monotonic, record):\n    pass\n"
            "def build_ms5a_owner_state(root, *, created_at, diagnostics):\n    pass\n",
            encoding="utf-8",
        )
        owner_bytes_for_binding = owner_source.read_bytes()
    market_data_root = tmp_path / "registered-data"
    market_data_root.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    cases = _cases()
    bindings = _bindings()
    bindings["runtime_file_sha256"][executor._OWNER_DIAGNOSTICS_MODULE] = hashlib.sha256(
        owner_bytes_for_binding
    ).hexdigest()
    attestation_order: list[str] = []
    monkeypatch.setattr(executor, "_select", lambda _root: (bindings, cases))
    monkeypatch.setattr(executor, "_bind_execution_identity", lambda value, *_args: dict(value))
    monkeypatch.setattr(executor, "_verify_runtime_strategy_factory_package", lambda _root: None)
    monkeypatch.setattr(executor, "_verify_runtime_module_origin", lambda *_args: None)

    def attest_repaired_modules(*_args: Any) -> dict[str, str]:
        attestation_order.append("repaired-modules")
        return _module_hashes()

    def attest_full_runtime(*_args: Any) -> dict[str, str]:
        attestation_order.append("full-runtime")
        return _module_hashes() | {"fake.runtime": "5" * 64}

    monkeypatch.setattr(executor, "_attest_repaired_mdi_modules", attest_repaired_modules)
    monkeypatch.setattr(executor, "_attest_runtime_modules", attest_full_runtime)
    monkeypatch.setattr(
        executor,
        "_load_verified_owner_module",
        lambda *_args: strategy_factory.multi_strategy_owner,
    )
    monkeypatch.setattr(executor, "_prefix_input_hash", lambda *_args: "6" * 64)
    monkeypatch.setattr(executor, "_native_specification", lambda value: value)

    request = SimpleNamespace(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        dataset=SimpleNamespace(
            sha256=bindings["dataset_sha256"],
            funding_sha256="7" * 64,
            dataset_id="registered-v6",
        ),
    )
    loaded_owner = SimpleNamespace(
        request=request,
        dataset_file_size=123,
        owner_row_count=456,
        funding_source=object(),
    )
    monkeypatch.setattr(
        strategy_factory.contract_codec,
        "decode_strategy_factory_request",
        lambda _payload: request,
    )

    def build_fake_owner(*_args: Any, created_at: datetime, diagnostics: Any) -> Any:
        del created_at, diagnostics
        attestation_order.append("owner-resolution")
        return loaded_owner

    def build_old_pinned_owner(*_args: Any, created_at: datetime) -> Any:
        del created_at
        pytest.fail("old pinned owner must be rejected before resolution")

    monkeypatch.setattr(
        strategy_factory.multi_strategy_owner,
        "build_ms5a_owner_state",
        build_old_pinned_owner if old_runtime else build_fake_owner,
    )
    if old_runtime:
        for name in (
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION",
            "create_owner_resolution_diagnostics",
        ):
            monkeypatch.delattr(strategy_factory.multi_strategy_owner, name, raising=False)
    else:
        monkeypatch.setattr(strategy_factory.multi_strategy_owner, "__file__", str(owner_source))
        monkeypatch.setattr(
            strategy_factory.multi_strategy_owner,
            "OWNER_DIAGNOSTICS_INTERFACE_VERSION",
            executor.OWNER_DIAGNOSTICS_INTERFACE_VERSION,
            raising=False,
        )
        monkeypatch.setattr(
            strategy_factory.multi_strategy_owner,
            "create_owner_resolution_diagnostics",
            strategy_factory.multi_strategy_owner.OwnerResolutionDiagnostics,
            raising=False,
        )

    def make_owner_state(
        *, request: Any, dataset_file_size: int, owner_row_count: int, funding_source: Any
    ) -> Any:
        return SimpleNamespace(
            request=request,
            dataset_file_size=dataset_file_size,
            owner_row_count=owner_row_count,
            funding_source=funding_source,
        )

    monkeypatch.setattr(
        strategy_factory.multi_strategy_owner,
        "OwnerState",
        make_owner_state,
    )
    monkeypatch.setattr(
        strategy_factory.multi_strategy_search_surface,
        "build_ms5a_search_surface",
        lambda **_kwargs: SimpleNamespace(trusted_runtime_modules=()),
    )
    run = SimpleNamespace(run_id="sf-run-4a2799648bcd52ca8ad7e185")
    run_roots: list[Path] = []

    def make_run(**kwargs: Any) -> Any:
        run_roots.append(kwargs["state_root"])
        return run

    monkeypatch.setattr(strategy_factory.run, "StrategyFactoryRun", make_run)
    output_roots: list[Path] = []

    class FakeConfigFactory:
        def __init__(self, **kwargs: Any) -> None:
            output_roots.append(kwargs["allowed_output_root"])

        def __call__(self, job: Any) -> Any:
            assert job.output_directory.is_relative_to(output_roots[-1])
            return job

    monkeypatch.setattr(
        strategy_factory.multi_strategy_research_runtime,
        "MS5ABacktestConfigFactory",
        FakeConfigFactory,
    )
    invocations: list[Any] = []

    class FakeFundingRunner:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["funding_source"] is loaded_owner.funding_source
            assert kwargs["execution_window_seconds"] > 0

        def run_with_performance_mode(self, config: Any, *, performance_mode: str) -> None:
            invocations.append((config, performance_mode))
            if failed:
                raise RuntimeError("synthetic callback failure")

    monkeypatch.setattr(
        strategy_factory.multi_strategy_research_runtime,
        "_OwnerFundingBacktestRunner",
        FakeFundingRunner,
    )

    class FakeResearchPlanBuilder:
        def __init__(self, root: Path) -> None:
            assert root.is_relative_to(scratch_root)

        def build(self, *, experiment_id: str, specification: Any) -> Any:
            case = next(item for item in cases if item.experiment_id == experiment_id)
            start = datetime(2024, 1, 1, tzinfo=UTC)
            job = modular_research_runner.engine.BacktestJob(
                run_id=f"job-{case.family}",
                experiment_id=experiment_id,
                stage_id="optimization_000",
                split_id="search-train",
                dataset_id="registered-v6",
                scenario_id="base",
                seed=312,
                parameters={"factory_run_id": run.run_id, "family": case.family},
                output_directory=scratch_root / "unused",
                stage_role="optimization_000",
                period=modular_research_runner.models.TimeRange(
                    start, start + timedelta(seconds=5 * 60 * 5_000)
                ),
            )
            # Later HOLDOUT-shaped work is present but must never be selected.
            holdout = SimpleNamespace(stage_role="holdout", run_id=f"holdout-{case.family}")
            return SimpleNamespace(jobs=(job, holdout))

    monkeypatch.setattr(modular_research_runner, "ResearchPlanBuilder", FakeResearchPlanBuilder)
    monkeypatch.setattr(
        modular_backtest, "run_backtest", lambda *_args, **_kwargs: None, raising=False
    )
    monkeypatch.setattr(modular_backtest.scalability, "PerformanceConfig", lambda **_kwargs: None)

    diagnostics = executor._DiagnosticLedger(scratch_root)
    if old_runtime or owner_path_escape:
        with pytest.raises(executor._ChildPhaseFailure) as raised:
            executor._child_run(
                runtime_source,
                runtime_root,
                market_data_root,
                scratch_root,
                bindings,
                diagnostics,
            )
        diagnostics.close()
        payload = _setup_failure_payload(raised.value)
        assert raised.value.phase == "runtime_interface"
        assert payload["setup_failure"]["error_class"] == "ExternalComponentUnavailableError"
        assert payload["runtime_module_sha256"] == _module_hashes()
        assert attestation_order == ["repaired-modules"]
        assert not any(market_data_root.iterdir())
        assert "old pinned" not in json.dumps(payload)
        assert not any(
            event["phase"] == "owner_resolution_started"
            for event in executor._collect_bootstrap_diagnostics(scratch_root, runtime_source)[
                "phase_events"
            ]
        )
        return

    report = executor._child_run(
        runtime_source, runtime_root, market_data_root, scratch_root, bindings, diagnostics
    )
    diagnostics.close()
    phase_events = executor._collect_bootstrap_diagnostics(scratch_root, runtime_source)[
        "phase_events"
    ]
    phases = [event["phase"] for event in phase_events]
    plan_phases = [phase for phase in phases if phase.startswith("plan_build_")]
    case_phases = [
        phase for phase in phases if phase in {"case_started", "case_operation_returned"}
    ]

    assert report["status"] == ("stopped" if failed else "completed")
    assert report["frozen_lineage_source_commit_sha"] == FROZEN_LINEAGE_SHA
    assert report["runtime_source_commit_sha"] == DIAGNOSTIC_RUNTIME_SHA
    assert report["candidate_metrics_included"] is False
    assert report["runtime_module_sha256"] == _module_hashes() | {"fake.runtime": "5" * 64}
    assert attestation_order == ["repaired-modules", "owner-resolution", "full-runtime"]
    assert len(report["cases"]) == (1 if failed else len(cases) * len(WINDOWS))
    assert plan_phases == ["plan_build_started", "plan_build_returned"] * len(cases)
    assert case_phases == ["case_started", "case_operation_returned"] * len(report["cases"])
    last_plan_returned = max(
        index for index, phase in enumerate(phases) if phase == "plan_build_returned"
    )
    assert phases.index("case_started") > last_plan_returned
    assert len(phase_events) <= executor.BOOTSTRAP_LEDGER_MAX_EVENTS
    assert all(mode == "research_fast" for _, mode in invocations)
    assert all(job.stage_role == "optimization_000" for job, _mode in invocations)
    assert all("holdout" not in job.run_id for job, _mode in invocations)
    assert all(job.seed == 312 for job, _mode in invocations)
    assert run_roots == [scratch_root / "metadata"]
    assert output_roots == [scratch_root / "case-results"]
    assert len(list((scratch_root / "profiles").glob("*.prof"))) == len(report["cases"])
    assert not list((scratch_root / "case-results").iterdir())
    assert {
        path.relative_to(runtime_root): path.read_bytes()
        for path in runtime_root.rglob("*")
        if path.is_file()
    } == runtime_before
    assert not any(market_data_root.iterdir())


def test_child_pipeline_refuses_parent_binding_drift_before_owner_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "frozen-runtime"
    runtime_root.mkdir()
    runtime_source = tmp_path / "accepted-source"
    runtime_source.mkdir()
    market_data_root = tmp_path / "registered-data"
    market_data_root.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    bindings = _bindings()
    monkeypatch.setattr(executor, "_select", lambda _root: (bindings, _cases()))
    monkeypatch.setattr(executor, "_bind_execution_identity", lambda value, *_args: dict(value))

    with pytest.raises(CompatibilityError, match="bindings differ"):
        executor._child_run(
            runtime_source,
            runtime_root,
            market_data_root,
            scratch_root,
            {**bindings, "dataset_sha256": "f" * 64},
        )

    assert not any(scratch_root.iterdir())
    assert not any(market_data_root.iterdir())


def test_prefix_input_hash_binds_registered_rows_and_rejects_truncated_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strategy_factory.multi_strategy_owner as owner

    rows = [
        SimpleNamespace(
            open_time=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
            open=1,
            high=2,
            low=0,
            close=1,
            volume=3,
        )
        for index in range(WINDOWS[0])
    ]
    selected: list[Any] = []

    def resolve(*args: Any, **kwargs: Any) -> str:
        selected.append((args, kwargs))
        return "registered-selection"

    monkeypatch.setattr(
        owner, "_market_data_api", lambda: (resolve, lambda *_a, **_kw: iter(rows), None, None)
    )
    start = rows[0].open_time
    end = start + timedelta(seconds=300 * WINDOWS[0])

    result = executor._prefix_input_hash(tmp_path, start, end, WINDOWS[0])

    expected = hashlib.sha256()
    for row in rows:
        values = (row.open_time.isoformat(), "1", "2", "0", "1", "3")
        expected.update(json.dumps(values, separators=(",", ":")).encode() + b"\n")
    assert result == expected.hexdigest()
    assert selected[0][0][1] == owner.MS5A_DATASET_ID
    assert selected[0][1]["consumer"] == "strategy_factory"

    monkeypatch.setattr(
        owner, "_market_data_api", lambda: (resolve, lambda *_a, **_kw: iter(rows[:-1]), None, None)
    )
    with pytest.raises(CompatibilityError, match="row count"):
        executor._prefix_input_hash(tmp_path, start, end, WINDOWS[0])


def test_runtime_module_attestation_hashes_only_modules_inside_accepted_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "accepted"
    runtime_root.mkdir()
    module_file = runtime_root / "owner.py"
    module_file.write_text("accepted source", encoding="utf-8")
    module = SimpleNamespace(__file__=str(module_file))
    monkeypatch.setattr(executor.importlib, "import_module", lambda _name: module)
    accepted_digest = hashlib.sha256(b"accepted source").hexdigest()
    monkeypatch.setattr(
        executor,
        "DIAGNOSTIC_RUNTIME_FILE_SHA256",
        {relative: accepted_digest for relative in DIAGNOSTIC_RUNTIME_FILE_SHA256},
    )
    surface = SimpleNamespace(
        trusted_runtime_modules=(SimpleNamespace(import_path="synthetic.module:run"),)
    )

    digests = executor._attest_runtime_modules(runtime_root, surface)

    assert len(digests) >= 1
    assert set(digests.values()) == {accepted_digest}
    outside = tmp_path / "outside.py"
    outside.write_text("unaccepted", encoding="utf-8")
    monkeypatch.setattr(
        executor.importlib, "import_module", lambda _name: SimpleNamespace(__file__=str(outside))
    )
    with pytest.raises(CompatibilityError, match="outside accepted source"):
        executor._attest_runtime_modules(runtime_root, surface)


def test_process_group_rss_sums_only_well_formed_rows_for_child_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        executor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="101 44 12\n102 99 500\n103 44 8\nbad row\n104 xx 20\n"
        ),
    )
    assert executor._process_group_rss_bytes(44) == 20 * 1024


def _write_cgroup(
    root: Path,
    proc_cgroup: Path,
    *,
    relative: str = "slice/job",
    memory_max: str = str(8 * 1024**3),
    swap_max: str = "0",
    memory_current: str = "1024",
) -> Path:
    group = root / relative
    group.mkdir(parents=True)
    (group / "memory.max").write_text(memory_max, encoding="ascii")
    (group / "memory.swap.max").write_text(swap_max, encoding="ascii")
    (group / "memory.current").write_text(memory_current, encoding="ascii")
    proc_cgroup.write_text(f"0::{relative}\n", encoding="utf-8")
    return group


def test_hard_memory_cap_reads_current_v2_group_and_reports_exact_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    proc_cgroup = tmp_path / "proc-cgroup"
    proc_cgroup.touch()
    maximum = 8 * 1024**3
    _write_cgroup(cgroup_root, proc_cgroup, memory_max=str(maximum))

    limits = executor.require_hard_memory_cap(
        cgroup_root=cgroup_root, proc_cgroup=proc_cgroup, maximum_bytes=maximum
    )

    assert limits == {"memory_max_bytes": maximum, "memory_swap_max_bytes": 0}


@pytest.mark.parametrize(
    ("memory_max", "swap_max", "memory_current"),
    [
        ("max", "0", "1024"),
        (str(8 * 1024**3 + 1), "0", "1024"),
        (str(8 * 1024**3), "1", "1024"),
        ("invalid", "0", "1024"),
        (str(8 * 1024**3), "0", str(8 * 1024**3 + 1)),
    ],
)
def test_hard_memory_cap_fails_closed_on_unbounded_or_inconsistent_cgroup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_max: str,
    swap_max: str,
    memory_current: str,
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    proc_cgroup = tmp_path / "proc-cgroup"
    proc_cgroup.touch()
    _write_cgroup(
        cgroup_root,
        proc_cgroup,
        memory_max=memory_max,
        swap_max=swap_max,
        memory_current=memory_current,
    )

    with pytest.raises(CompatibilityError):
        executor.require_hard_memory_cap(
            cgroup_root=cgroup_root,
            proc_cgroup=proc_cgroup,
            maximum_bytes=8 * 1024**3,
        )


def test_hard_memory_cap_rejects_unsafe_or_non_unified_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    proc_cgroup = tmp_path / "proc-cgroup"
    proc_cgroup.write_text("5:cpu:/legacy\n", encoding="utf-8")

    with pytest.raises(CompatibilityError, match="unavailable"):
        executor.require_hard_memory_cap(cgroup_root=cgroup_root, proc_cgroup=proc_cgroup)

    proc_cgroup.write_text("0::../outside\n", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="unsafe"):
        executor.require_hard_memory_cap(cgroup_root=cgroup_root, proc_cgroup=proc_cgroup)

    proc_cgroup.write_text("0::slice/job\n", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="inputs are invalid"):
        executor.require_hard_memory_cap(
            cgroup_root=cgroup_root, proc_cgroup=proc_cgroup, maximum_bytes=0
        )


def test_hard_memory_cap_rejects_group_escape_symlinks_and_missing_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    root = tmp_path / "cgroup"
    root.mkdir()
    proc_cgroup = tmp_path / "proc-cgroup"
    proc_cgroup.write_text("0::slice/job\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "slice").mkdir()
    (root / "slice/job").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="unavailable"):
        executor.require_hard_memory_cap(cgroup_root=root, proc_cgroup=proc_cgroup)

    (root / "slice/job").unlink()
    (root / "slice/job").mkdir()
    with pytest.raises(CompatibilityError, match="unavailable"):
        executor.require_hard_memory_cap(cgroup_root=root, proc_cgroup=proc_cgroup)


def test_execute_profile_hard_cap_refusal_happens_before_claim_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    cases, calls = _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())

    def reject_cap() -> dict[str, int]:
        raise CompatibilityError("hard cgroup cap unavailable")

    monkeypatch.setattr(executor, "require_hard_memory_cap", reject_cap)
    with pytest.raises(CompatibilityError, match="hard cgroup cap"):
        executor.execute_profile(**paths)

    assert len(cases) == len(FAMILIES)
    assert not (paths["claim_root"] / "consumed.json").exists()
    assert not calls
    assert not paths["report_path"].exists()


def _write_outer_watchdog_proc(
    proc_root: Path,
    *,
    executable: str = "timeout",
    argv: tuple[str, ...] | None = None,
) -> None:
    pid_one = proc_root / "1"
    pid_one.mkdir(parents=True)
    executable_path = proc_root.parent / "bin" / executable
    executable_path.parent.mkdir(parents=True, exist_ok=True)
    executable_path.touch()
    (pid_one / "exe").symlink_to(executable_path)
    if argv is None:
        argv = (
            "/usr/bin/timeout",
            "--signal=KILL",
            "1200s",
            "/usr/local/bin/python",
            "-B",
            "-m",
            "strategy_factory.ms5a_profile_executor",
            "--runtime-root",
            "/runtime",
            "--runtime-source",
            "/runtime/src",
            "--market-data-root",
            "/market-data",
            "--authority-manifest",
            "/grant.json",
            "--claim-root",
            "/claims",
            "--scratch-root",
            "/scratch",
            "--report",
            "/report.json",
        )
    (pid_one / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_outer_watchdog_accepts_only_expected_pid1_timeout_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    proc_root = tmp_path / "proc"
    _write_outer_watchdog_proc(proc_root)

    assert executor.require_outer_watchdog(proc_root=proc_root) == {
        "pid1_executable": "timeout",
        "outer_timeout_seconds": 1200,
        "outer_signal": "KILL",
    }


@pytest.mark.parametrize(
    ("platform", "timeout_seconds"),
    [("darwin", 1200), ("linux", 1201)],
)
def test_outer_watchdog_rejects_unsupported_platform_or_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    timeout_seconds: int,
) -> None:
    monkeypatch.setattr(executor.sys, "platform", platform)

    with pytest.raises(CompatibilityError, match="Linux PID-1 outer watchdog"):
        executor.require_outer_watchdog(
            proc_root=tmp_path / "proc", timeout_seconds=timeout_seconds
        )


@pytest.mark.parametrize("missing", ["argv", "option_value"])
def test_outer_watchdog_rejects_incomplete_pid1_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    proc_root = tmp_path / "proc"
    if missing == "argv":
        _write_outer_watchdog_proc(
            proc_root,
            argv=("/usr/bin/timeout", "--signal=KILL", "1200s", "/usr/local/bin/python"),
        )
        expected_message = "command is incomplete"
    else:
        _write_outer_watchdog_proc(proc_root)
        cmdline = proc_root / "1" / "cmdline"
        argv = cmdline.read_bytes().split(b"\0")[:-2]
        cmdline.write_bytes(b"\0".join(argv) + b"\0")
        expected_message = "args are incomplete"

    with pytest.raises(CompatibilityError, match=expected_message):
        executor.require_outer_watchdog(proc_root=proc_root)


@pytest.mark.parametrize(
    ("period", "stage_role"),
    [(None, "optimization_000"), (SimpleNamespace(start=None, end=None), "train")],
)
def test_prefix_job_rejects_unfrozen_stage_before_rewriting_job(
    period: Any, stage_role: str, tmp_path: Path
) -> None:
    job = SimpleNamespace(period=period, stage_role=stage_role)

    with pytest.raises(CompatibilityError, match="outside first frozen SEARCH stage"):
        executor._prefix_job(job, 1_000, tmp_path / "case-output")


def test_kill_group_does_not_signal_an_already_exited_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=42, poll=lambda: 0)
    monkeypatch.setattr(executor.os, "killpg", lambda *_args: pytest.fail("must not signal"))

    executor._kill_group(process)  # type: ignore[arg-type]


def test_child_launcher_preserves_buffered_terminal_output_after_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    terminal_receipt = b'{"status":"completed"}\n'

    class FinishedProcess:
        pid = 42
        returncode: int | None = None
        stdout = object()

        def __init__(self) -> None:
            self.poll_count = 0

        def poll(self) -> int | None:
            self.poll_count += 1
            if self.poll_count == 1:
                return None
            self.returncode = 0
            return 0

    class BufferedSelector:
        def register(self, *_args: Any) -> None:
            return None

        def select(self, *, timeout: float) -> list[tuple[Any, Any]]:
            if timeout:
                return []
            return [(SimpleNamespace(fd=42), executor.selectors.EVENT_READ)]

        def close(self) -> None:
            return None

    process = FinishedProcess()
    selector = BufferedSelector()
    monkeypatch.setattr(executor.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(executor.os, "read", lambda _fd, _size: terminal_receipt)

    result = executor.launch_bounded_child(
        ("synthetic-child",),
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
        popen=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
        rss_reader=lambda _pid: 0,
        monotonic=lambda: 0.0,
        sleep=lambda _delay: None,
    )

    assert result == ("completed", 0, "", terminal_receipt.decode())


@pytest.mark.parametrize(
    ("executable", "arg_change"),
    [
        ("python", None),
        ("timeout", (1, "--signal=TERM")),
        ("timeout", (2, "1201s")),
        ("timeout", (3, "/usr/local/bin/node")),
        ("timeout", (3, "/usr/local/bin/pythonx")),
        ("timeout", (5, "--no-module")),
        ("timeout", (6, "untrusted.executor")),
        ("timeout", (7, "--plan-only")),
        ("timeout", (9, "/elsewhere/runtime")),
    ],
)
def test_outer_watchdog_fails_closed_on_wrong_pid1_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable: str,
    arg_change: tuple[int, str] | None,
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    proc_root = tmp_path / "proc"
    argv = [
        "/usr/bin/timeout",
        "--signal=KILL",
        "1200s",
        "/usr/local/bin/python",
        "-B",
        "-m",
        "strategy_factory.ms5a_profile_executor",
        "--runtime-root",
        "/runtime",
        "--runtime-source",
        "/runtime/src",
        "--market-data-root",
        "/market-data",
        "--authority-manifest",
        "/grant.json",
        "--claim-root",
        "/claims",
        "--scratch-root",
        "/scratch",
        "--report",
        "/report.json",
    ]
    if arg_change is not None:
        argv[arg_change[0]] = arg_change[1]
    _write_outer_watchdog_proc(proc_root, executable=executable, argv=tuple(argv))

    with pytest.raises(CompatibilityError):
        executor.require_outer_watchdog(proc_root=proc_root)


def test_outer_watchdog_accepts_python_versioned_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    proc_root = tmp_path / "proc"
    argv = [
        "/usr/bin/timeout",
        "--signal=KILL",
        "1200s",
        "/usr/local/bin/python3.13",
        "-B",
        "-m",
        "strategy_factory.ms5a_profile_executor",
        "--runtime-root",
        "/runtime",
        "--runtime-source",
        "/runtime/src",
        "--market-data-root",
        "/market-data",
        "--authority-manifest",
        "/grant.json",
        "--claim-root",
        "/claims",
        "--scratch-root",
        "/scratch",
        "--report",
        "/report.json",
    ]
    _write_outer_watchdog_proc(proc_root, argv=tuple(argv))

    assert executor.require_outer_watchdog(proc_root=proc_root)["outer_timeout_seconds"] == 1200


@pytest.mark.parametrize("missing", ["exe", "cmdline"])
def test_outer_watchdog_fails_closed_when_pid1_evidence_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(executor.sys, "platform", "linux")
    proc_root = tmp_path / "proc"
    _write_outer_watchdog_proc(proc_root)
    (proc_root / "1" / missing).unlink()

    with pytest.raises(CompatibilityError):
        executor.require_outer_watchdog(proc_root=proc_root)


def test_execute_profile_outer_watchdog_refusal_happens_before_claim_or_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())

    def reject_watchdog(**_kwargs: Any) -> dict[str, str | int]:
        raise CompatibilityError("PID 1 timeout guard is unavailable")

    monkeypatch.setattr(executor, "require_outer_watchdog", reject_watchdog)
    with pytest.raises(CompatibilityError, match="PID 1 timeout"):
        executor.execute_profile(**paths)

    assert not (paths["claim_root"] / "consumed.json").exists()
    assert not paths["report_path"].exists()


def test_execute_profile_outer_deadline_refuses_before_claim_or_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    times = iter((0.0, float(executor.OUTER_HARD_SECONDS)))
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(times))

    with pytest.raises(TimeoutError, match="before launch"):
        executor.execute_profile(**paths)

    assert not (paths["claim_root"] / "consumed.json").exists()
    assert not paths["report_path"].exists()


def test_execute_profile_burns_grant_if_child_process_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _execution_paths(tmp_path)
    _install_parent_stubs(monkeypatch, paths, child_stdout=_child_receipt())
    launches: list[int] = []

    def fail_to_launch(*_args: Any, **_kwargs: Any) -> Any:
        launches.append(1)
        raise OSError("synthetic process start failure")

    monkeypatch.setattr(executor, "launch_bounded_child", fail_to_launch)
    with pytest.raises(OSError, match="process start"):
        executor.execute_profile(**paths)

    claim = paths["claim_root"] / "consumed.json"
    assert claim.is_file()
    with pytest.raises(FileExistsError):
        executor.execute_profile(**paths)
    assert launches == [1]
    assert not paths["report_path"].exists()


def test_kill_group_escalates_only_after_grace_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 444

        def __init__(self, *, timeout: bool) -> None:
            self.timeout = timeout
            self.wait_calls: list[float] = []

        def poll(self) -> int | None:
            return None

        def wait(self, *, timeout: float) -> None:
            self.wait_calls.append(timeout)
            if self.timeout and timeout == 0.5:
                raise subprocess.TimeoutExpired("child", timeout)

    groups: list[tuple[int, int]] = []
    monkeypatch.setattr(executor.os, "killpg", lambda pid, sig: groups.append((pid, sig)))
    process = FakeProcess(timeout=True)

    executor._kill_group(process)

    assert groups == [(444, signal.SIGTERM), (444, signal.SIGKILL)]
    assert process.wait_calls == [0.5, 2]


def test_kill_group_handles_disappearing_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    class RunningProcess:
        pid = 555

        def poll(self) -> None:
            return None

    monkeypatch.setattr(
        executor.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    executor._kill_group(RunningProcess())


def test_safe_directory_and_scratch_refuse_missing_files_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(CompatibilityError, match="unavailable"):
        executor._safe_directory(tmp_path / "missing", must_exist=True)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CompatibilityError, match="symlink"):
        executor._safe_directory(link, must_exist=True)
    with pytest.raises(CompatibilityError, match="symlink"):
        executor._prepare_scratch(link)

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="empty"):
        executor._prepare_scratch(file_path)

    new_scratch = tmp_path / "new-scratch"
    assert executor._prepare_scratch(new_scratch) == new_scratch.resolve()


def test_runtime_checkout_and_selection_fail_closed_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outputs = iter(
        [
            SimpleNamespace(stdout=DIAGNOSTIC_RUNTIME_SHA + "\n"),
            SimpleNamespace(stdout=" M frozen-request.json\n"),
        ]
    )
    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: next(outputs))
    with pytest.raises(CompatibilityError, match="exact and clean"):
        executor._verify_runtime_checkout(runtime)

    monkeypatch.setattr(
        executor,
        "select_frozen_cases",
        lambda _root: ({"runtime_surface_fingerprint": "wrong"}, _cases()),
    )
    with pytest.raises(CompatibilityError, match="runtime surface"):
        executor._select(runtime)

    monkeypatch.setattr(
        executor,
        "select_frozen_cases",
        lambda _root: (_bindings(), _cases()),
    )
    assert executor._select(runtime) == (_bindings(), _cases())


def test_runtime_checkout_accepts_exact_clean_source_and_rejects_wrong_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outputs = iter(
        [
            SimpleNamespace(stdout=DIAGNOSTIC_RUNTIME_SHA + "\n"),
            SimpleNamespace(stdout=""),
        ]
    )
    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: next(outputs))
    monkeypatch.setattr(
        executor,
        "_runtime_file_sha256",
        lambda _root: dict(DIAGNOSTIC_RUNTIME_FILE_SHA256),
    )
    assert executor._verify_runtime_checkout(runtime) == runtime.resolve()

    outputs = iter(
        [
            SimpleNamespace(stdout="e" * 40 + "\n"),
            SimpleNamespace(stdout=""),
        ]
    )
    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: next(outputs))
    with pytest.raises(CompatibilityError, match="exact and clean"):
        executor._verify_runtime_checkout(runtime)


def test_runtime_checkout_rejects_helper_digest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outputs = iter(
        [
            SimpleNamespace(stdout=DIAGNOSTIC_RUNTIME_SHA + "\n"),
            SimpleNamespace(stdout=""),
        ]
    )
    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: next(outputs))
    wrong_hashes = dict(DIAGNOSTIC_RUNTIME_FILE_SHA256)
    first_path = next(iter(wrong_hashes))
    wrong_hashes[first_path] = "f" * 64
    monkeypatch.setattr(executor, "_runtime_file_sha256", lambda _root: wrong_hashes)

    with pytest.raises(CompatibilityError, match="source digest is invalid"):
        executor._verify_runtime_checkout(runtime)


def test_runtime_file_hashes_are_computed_from_contained_synthetic_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    sources = {
        "modules/market_data_ingestion/src/market_data_ingestion/integrity.py": b"integrity",
        "modules/market_data_ingestion/src/market_data_ingestion/storage.py": b"storage",
    }
    expected = {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}
    monkeypatch.setattr(executor, "DIAGNOSTIC_RUNTIME_FILE_SHA256", expected)
    for relative, content in sources.items():
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert executor._runtime_file_sha256(runtime) == expected


def test_runtime_file_hash_rejects_symlink_escape_from_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    relative = "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "integrity.py").write_bytes(b"synthetic outside source")
    link = runtime / "modules/market_data_ingestion/src/market_data_ingestion"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    monkeypatch.setattr(
        executor,
        "DIAGNOSTIC_RUNTIME_FILE_SHA256",
        {relative: "0" * 64},
    )

    with pytest.raises(CompatibilityError, match="escaped checkout"):
        executor._runtime_file_sha256(runtime)


def test_controller_checkout_rejects_wrong_or_dirty_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = tmp_path / "controller"
    controller.mkdir()
    outputs = iter(
        [
            SimpleNamespace(stdout="d" * 40 + "\n"),
            SimpleNamespace(stdout=""),
        ]
    )
    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: next(outputs))
    assert executor._verify_controller_checkout(controller, "d" * 40) == "d" * 40

    outputs = iter(
        [
            SimpleNamespace(stdout="d" * 40 + "\n"),
            SimpleNamespace(stdout=" M protocol.py\n"),
        ]
    )
    monkeypatch.setattr(executor.subprocess, "run", lambda *_args, **_kwargs: next(outputs))
    with pytest.raises(CompatibilityError, match="controller checkout is not exact and clean"):
        executor._verify_controller_checkout(controller, "d" * 40)


def test_source_import_paths_include_runtime_only_and_omit_controller(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    controller = tmp_path / "controller"
    for relative in (
        "research/strategy_factory/src",
        "modules/sample/src",
    ):
        (runtime / relative).mkdir(parents=True)
    expected_controller = controller / "research/strategy_factory/src"
    expected_controller.mkdir(parents=True)

    paths = executor._source_import_paths(runtime, controller)

    assert str((runtime / "research/strategy_factory/src").resolve()) in paths
    assert str((runtime / "modules/sample/src").resolve()) in paths
    assert str(expected_controller.resolve()) not in paths
    assert str((runtime / "src").resolve()) not in paths

    command = executor._child_command(
        runtime,
        controller,
        (),
        _bindings()["controller_module_sha256"],
        _bindings()["runtime_controller_api_sha256"],
    )
    bootstrap = command[-1]
    assert "strategy_factory.__path__.append" not in bootstrap
    assert "_ms5a_controller.strategy_factory" in bootstrap
    assert (
        str(expected_controller.resolve())
        not in bootstrap.split("runtime_paths = ", 1)[1].split("\n", 1)[0]
    )


def _synthetic_loader_inputs(
    tmp_path: Path,
    *,
    controller_overrides: dict[str, str] | None = None,
) -> tuple[Path, Path, dict[str, str], dict[str, str]]:
    controller = tmp_path / "controller"
    runtime = tmp_path / "runtime"
    controller_src = controller / "research/strategy_factory/src"
    runtime_src = runtime / "research/strategy_factory/src"
    (controller_src / "strategy_factory").mkdir(parents=True)
    package_dir = runtime_src / "strategy_factory"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("RUNTIME_PACKAGE = True\n", encoding="utf-8")
    for name, relative in executor._RUNTIME_CONTROLLER_API_MODULES.items():
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"RUNTIME_MODULE = {name!r}\n", encoding="utf-8")
    controller_hashes: dict[str, str] = {}
    for name, relative in executor._CONTROLLER_MODULES.items():
        path = controller / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        source = (controller_overrides or {}).get(name, f"CONTROLLER_MODULE = {name!r}\n")
        path.write_text(source, encoding="utf-8")
        controller_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    runtime_hashes = {
        name: hashlib.sha256((runtime / relative).read_bytes()).hexdigest()
        for name, relative in executor._RUNTIME_CONTROLLER_API_MODULES.items()
    }
    return controller, runtime, controller_hashes, runtime_hashes


def _run_synthetic_controller_loader(
    controller: Path,
    runtime: Path,
    controller_hashes: dict[str, str],
    runtime_hashes: dict[str, str],
    *,
    controller_src_on_pythonpath: bool = True,
) -> subprocess.CompletedProcess[str]:
    import os
    import sys

    runtime_src = runtime / "research/strategy_factory/src"
    loader = executor._controller_bootstrap_source(
        runtime_paths=[str(runtime_src)],
        controller_root=controller,
        args=(),
        controller_module_sha256=controller_hashes,
        runtime_controller_api_sha256=runtime_hashes,
        dispatch=False,
    )
    env = {"PATH": os.environ.get("PATH", "")}
    if controller_src_on_pythonpath:
        env["PYTHONPATH"] = str(controller / "research/strategy_factory/src")
    return subprocess.run(
        (sys.executable, "-B", "-c", loader),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_controller_bootstrap_with_dispatch_compiles_without_running_child(
    tmp_path: Path,
) -> None:
    controller, runtime, controller_hashes, runtime_hashes = _synthetic_loader_inputs(tmp_path)
    loader = executor._controller_bootstrap_source(
        runtime_paths=[str(runtime / "research/strategy_factory/src")],
        controller_root=controller,
        args=("synthetic-argument",),
        controller_module_sha256=controller_hashes,
        runtime_controller_api_sha256=runtime_hashes,
        dispatch=True,
    )

    compile(loader, "<synthetic-controller-bootstrap>", "exec")
    assert '_child_main(["synthetic-argument"])' in loader


def test_controller_bootstrap_rejects_empty_runtime_paths_before_package_import(
    tmp_path: Path,
) -> None:
    import os
    import sys

    controller, _runtime, controller_hashes, runtime_hashes = _synthetic_loader_inputs(tmp_path)
    fallback_root = tmp_path / "fallback"
    fallback_package = fallback_root / "strategy_factory"
    fallback_package.mkdir(parents=True)
    marker = tmp_path / "fallback-package-imported"
    (fallback_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    loader = executor._controller_bootstrap_source(
        runtime_paths=[],
        controller_root=controller,
        args=(),
        controller_module_sha256=controller_hashes,
        runtime_controller_api_sha256=runtime_hashes,
        dispatch=False,
    )

    result = subprocess.run(
        (sys.executable, "-B", "-c", loader),
        check=False,
        capture_output=True,
        text=True,
        cwd=fallback_root,
        env={"PATH": os.environ.get("PATH", "")},
    )

    assert result.returncode != 0
    assert "accepted runtime source paths are unavailable" in result.stderr
    assert not marker.exists()


def test_controller_private_namespace_uses_runtime_contract_types_and_bound_source(
    tmp_path: Path,
) -> None:
    controller, runtime, controller_hashes, runtime_hashes = _synthetic_loader_inputs(
        tmp_path,
        controller_overrides={"ms5a_budget_probe": "from .contracts import RUNTIME_MODULE\n"},
    )
    loader = executor._controller_bootstrap_source(
        runtime_paths=[str(runtime / "research/strategy_factory/src")],
        controller_root=controller,
        args=(),
        controller_module_sha256=controller_hashes,
        runtime_controller_api_sha256=runtime_hashes,
        dispatch=False,
    )
    loader += (
        "\nassert sys.modules['_ms5a_controller'].strategy_factory.canonical is sys.modules['strategy_factory'].canonical\n"
        "assert sys.modules['_ms5a_controller'].strategy_factory.errors is sys.modules['strategy_factory.errors']\n"
        "assert sys.modules['_ms5a_controller'].strategy_factory.contracts is sys.modules['strategy_factory'].contracts\n"
        "assert sys.modules['_ms5a_controller'].strategy_factory.contract_codec is sys.modules['strategy_factory.contract_codec']\n"
        "assert '_ms5a_controller.strategy_factory.ms5a_profile_executor' in sys.modules\n"
        "assert sys.modules['_ms5a_controller.strategy_factory.ms5a_budget_probe'].RUNTIME_MODULE == 'contracts'\n"
        "assert 'strategy_factory.ms5a_profile_executor' not in sys.modules\n"
        "assert all(not _is_controller_path(item) for item in sys.path)\n"
        "assert all(not _is_controller_path(item) for item in sys.modules['strategy_factory'].__path__)\n"
    )
    import os
    import sys

    result = subprocess.run(
        (sys.executable, "-B", "-c", loader),
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(controller / "research/strategy_factory/src"),
        },
    )

    assert result.returncode == 0, result.stderr


def test_controller_loader_rejects_unknown_relative_helper(tmp_path: Path) -> None:
    controller, runtime, controller_hashes, runtime_hashes = _synthetic_loader_inputs(
        tmp_path,
        controller_overrides={"ms5a_budget_probe": "from .not_allowlisted import VALUE\n"},
    )

    result = _run_synthetic_controller_loader(
        controller, runtime, controller_hashes, runtime_hashes
    )

    assert result.returncode != 0
    assert "not_allowlisted" in result.stderr


def test_controller_loader_cannot_resolve_a_missing_runtime_dependency_from_controller(
    tmp_path: Path,
) -> None:
    controller, runtime, controller_hashes, runtime_hashes = _synthetic_loader_inputs(
        tmp_path,
        controller_overrides={
            "ms5a_profile_executor": "from strategy_factory import controller_only_helper\n"
        },
    )
    controller_helper = (
        controller / "research/strategy_factory/src/strategy_factory/controller_only_helper.py"
    )
    controller_helper.write_text("VALUE = True\n", encoding="utf-8")

    result = _run_synthetic_controller_loader(
        controller, runtime, controller_hashes, runtime_hashes
    )

    assert result.returncode != 0
    assert "controller_only_helper" in result.stderr


def test_write_report_replaces_atomically_and_refuses_symlink(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    executor._write_report(report, {"status": "safe"})
    assert json.loads(report.read_text()) == {"status": "safe"}
    assert not report.with_name("report.json.tmp").exists()

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(CompatibilityError, match="unsafe"):
        executor._write_report(link, {"status": "blocked"})


def test_empty_result_tree_has_stable_empty_hash(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert executor._tree_sha256(missing) == hashlib.sha256().hexdigest()
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.json").write_text("{}", encoding="utf-8")
    expected = hashlib.sha256()
    expected.update(b"result.json\0")
    expected.update(hashlib.sha256(b"{}").digest())
    assert executor._tree_sha256(output) == expected.hexdigest()


def test_plan_only_report_binds_plan_hashes_and_exact_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = _cases()
    monkeypatch.setattr(executor, "_verify_runtime_checkout", lambda path: path)
    monkeypatch.setattr(executor, "_bind_execution_identity", lambda value, *_args: dict(value))
    monkeypatch.setattr(executor, "_select", lambda _root: (_bindings(), cases))

    report = executor.plan_only_report(tmp_path, tmp_path, tmp_path)

    assert report["real_backtest_executed"] is False
    assert report["candidate_metrics_included"] is False
    assert len(report["execution_order"]) == len(cases) * len(WINDOWS)
    assert report["execution_order"][0] == {
        "family": FAMILIES[0],
        "bars": WINDOWS[0],
        "stage_role": "optimization_000",
    }
    assert report["execution_order"][-1]["bars"] == WINDOWS[-1]
    assert report["case_plan_hashes"] == [case.public() for case in cases]
    assert report["bindings"]["source_commit_sha"] == SOURCE_SHA
    assert report["bindings"]["frozen_lineage_source_commit_sha"] == FROZEN_LINEAGE_SHA
    assert report["bindings"]["runtime_source_commit_sha"] == DIAGNOSTIC_RUNTIME_SHA


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"reason_code":"CHILD_TIMEOUT"}', "CHILD_TIMEOUT"),
        ('{"status":"failed"}', "CHILD_FAILED"),
        ("not-json", "CHILD_FAILED_WITHOUT_RECEIPT"),
        ("", "CHILD_FAILED_WITHOUT_RECEIPT"),
    ],
)
def test_child_failure_reason_is_reduced_to_uppercase_code(stdout: str, expected: str) -> None:
    assert executor._child_reason_code(stdout) == expected


def test_child_watchdog_reports_failure_code_and_unavailable_rss_as_blocked(
    tmp_path: Path,
) -> None:
    import sys

    failed = executor.launch_bounded_child(
        (
            sys.executable,
            "-c",
            'import sys; print(\'{"reason_code":"SYNTHETIC_FAILURE"}\'); sys.exit(1)',
        ),
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
        rss_reader=lambda _pid: 0,
    )
    assert failed[:3] == ("child_failed", 1, "SYNTHETIC_FAILURE")

    def unavailable(_pid: int) -> int:
        raise OSError("synthetic RSS read failure")

    blocked = executor.launch_bounded_child(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
        rss_reader=unavailable,
    )
    assert blocked[0] == "watchdog_error"
    assert blocked[2] == "RSS_WATCHDOG_UNAVAILABLE"
    assert blocked[1] not in (None, 0)


def test_sanitized_receipt_stops_after_failure_and_rejects_contract_tampering() -> None:
    stopped = json.loads(_child_receipt(failed=True))
    sanitized = executor._sanitized_child_receipt(json.dumps(stopped))
    assert len(sanitized["cases"]) == 1
    assert sanitized["cases"][0]["status"] == "failed"

    mutations = (
        lambda value: value.update(probe="wrong"),
        lambda value: value.update(runtime_source_commit_sha="f" * 40),
        lambda value: value.update(frozen_lineage_source_commit_sha="f" * 40),
        lambda value: value.update(runtime_repair_id="unreviewed-repair"),
        lambda value: value["runtime_file_sha256"].update(
            {next(iter(value["runtime_file_sha256"])): "f" * 64}
        ),
        lambda value: value.update(controller_commit_sha="e" * 40),
        lambda value: value.update(cases=None),
        lambda value: value["cases"][0].update(stage_role="holdout"),
        lambda value: value["cases"][0].update(duration_seconds=True),
        lambda value: value["cases"][0].update(input_sha256="bad"),
        lambda value: value.update(runtime_module_sha256={}),
        lambda value: value.update(runtime_module_sha256={"module": "bad"}),
        lambda value: value["runtime_module_sha256"].update(
            {"market_data_ingestion.integrity": "f" * 64}
        ),
    )
    for mutate in mutations:
        payload = json.loads(_child_receipt())
        mutate(payload)
        with pytest.raises(CompatibilityError):
            executor._sanitized_child_receipt(json.dumps(payload), _bindings())


def test_child_main_emits_sanitized_success_and_error_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_source = tmp_path / "accepted"
    runtime_source.mkdir()
    monkeypatch.setattr(executor, "_verify_runtime_checkout", lambda path: path)
    monkeypatch.setattr(executor, "_verify_controller_checkout", lambda *_args, **_kwargs: "d" * 40)
    monkeypatch.setattr(executor, "_child_run", lambda *_args: {"status": "completed", "cases": []})

    def child_args(bindings_json: str = "{}") -> list[str]:
        scratch = tmp_path / f"scratch-{len(list(tmp_path.glob('scratch-*')))}"
        scratch.mkdir()
        return [
            "--child",
            "--runtime-root",
            str(tmp_path),
            "--market-data-root",
            str(tmp_path),
            "--runtime-source",
            str(runtime_source),
            "--scratch-root",
            str(scratch),
            "--bindings-json",
            bindings_json,
        ]

    assert executor._child_main(child_args()) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "cases": [],
    }

    monkeypatch.setattr(
        executor, "_child_run", lambda *_args: (_ for _ in ()).throw(RuntimeError("private path"))
    )
    assert executor._child_main(child_args()) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason_code": "RUNTIMEERROR",
    }

    monkeypatch.setattr(executor, "_child_run", lambda *_args: {"status": "stopped"})
    assert executor._child_main(child_args()) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "stopped"}

    monkeypatch.setattr(executor, "_child_run", lambda *_args: pytest.fail("must not run"))
    assert executor._child_main(child_args("[]")) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason_code": "COMPATIBILITYERROR",
    }


def test_wrapped_owner_failure_is_sanitized_through_parent_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_source = tmp_path / "accepted"
    runtime_source.mkdir()
    monkeypatch.setattr(executor, "_verify_runtime_checkout", lambda path: path)
    monkeypatch.setattr(executor, "_verify_controller_checkout", lambda *_args, **_kwargs: "d" * 40)

    def fail_owner(*_args: Any) -> None:
        try:
            raise sqlite3.OperationalError("/private/catalog/owner.sqlite3")
        except sqlite3.OperationalError as cause:
            try:
                raise ExternalComponentUnavailableError("private mount /private/catalog") from cause
            except ExternalComponentUnavailableError as wrapped:
                raise executor._ChildPhaseFailure(
                    "owner_resolution", wrapped, _module_hashes()
                ) from wrapped

    monkeypatch.setattr(executor, "_child_run", fail_owner)
    child_args = [
        "--child",
        "--runtime-root",
        str(tmp_path),
        "--market-data-root",
        str(tmp_path),
        "--runtime-source",
        str(runtime_source),
        "--scratch-root",
        str(tmp_path),
        "--bindings-json",
        json.dumps(_bindings()),
    ]
    assert executor._child_main(child_args) == 1
    child_stdout = capsys.readouterr().out
    child_payload = json.loads(child_stdout)
    assert child_payload["setup_failure"] == {
        "phase": "owner_resolution",
        "dataset_kind": "ohlcv_funding_mark_price",
        "error_class": "ExternalComponentUnavailableError",
        "cause_classes": ["OperationalError"],
    }
    assert "/private" not in child_stdout

    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    paths = _execution_paths(parent_root)
    _install_parent_stubs(monkeypatch, paths, child_stdout=child_stdout)
    monkeypatch.setattr(
        executor,
        "launch_bounded_child",
        lambda *_args, **_kwargs: (
            "child_failed",
            1,
            "EXTERNALCOMPONENTUNAVAILABLEERROR",
            child_stdout,
        ),
    )

    report = executor.execute_profile(**paths)

    assert report["status"] == "child_failed"
    assert report["reason_code"] == "EXTERNALCOMPONENTUNAVAILABLEERROR"
    assert report["setup_failure"] == child_payload["setup_failure"]
    assert report["cases"] == []
    assert report["runtime_module_sha256"] == _module_hashes()
    assert report["claim_consumed"] is True
    assert "/private" not in json.dumps(report)


def test_setup_receipt_accepts_allowlisted_sqlite_data_error_cause() -> None:
    try:
        raise sqlite3.DataError("private database details")
    except sqlite3.DataError as cause:
        try:
            raise ExternalComponentUnavailableError("private owner details") from cause
        except ExternalComponentUnavailableError as wrapped:
            receipt = _setup_failure_payload(
                executor._ChildPhaseFailure("owner_resolution", wrapped)
            )

    sanitized = executor._sanitized_child_receipt(json.dumps(receipt))
    assert sanitized["setup_failure"]["cause_classes"] == ["DataError"]
    assert "private" not in json.dumps(sanitized)


def test_setup_failure_preserves_allowlisted_implicit_exception_context() -> None:
    try:
        raise sqlite3.OperationalError("private catalog details")
    except sqlite3.OperationalError:
        try:
            raise ExternalComponentUnavailableError("private resolver details")
        except ExternalComponentUnavailableError as wrapped:
            assert wrapped.__cause__ is None
            receipt = _setup_failure_payload(
                executor._ChildPhaseFailure("owner_resolution", wrapped)
            )

    sanitized = executor._sanitized_child_receipt(json.dumps(receipt))
    assert sanitized["setup_failure"]["cause_classes"] == ["OperationalError"]
    assert "private" not in json.dumps(sanitized)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda value: value.update(unexpected=True), id="extra-receipt-field"),
        pytest.param(
            lambda value: value["setup_failure"].update(unexpected=True),
            id="extra-metadata-field",
        ),
        pytest.param(lambda value: value["setup_failure"].update(phase=7), id="phase-wrong-type"),
        pytest.param(
            lambda value: value["setup_failure"].update(error_class=["PrivateError"]),
            id="error-class-wrong-type",
        ),
        pytest.param(
            lambda value: value["setup_failure"].update(cause_classes="OperationalError"),
            id="causes-wrong-type",
        ),
        pytest.param(
            lambda value: value["setup_failure"].update(cause_classes=[None]),
            id="cause-element-wrong-type",
        ),
    ],
)
def test_setup_failure_receipt_rejects_unexpected_fields_and_wrong_types(
    mutate: Any,
) -> None:
    receipt = _setup_failure_payload(
        executor._ChildPhaseFailure(
            "owner_resolution", ExternalComponentUnavailableError("private mount details")
        )
    )
    mutate(receipt)

    with pytest.raises(CompatibilityError):
        executor._sanitized_child_receipt(json.dumps(receipt))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda value: value.update(cases=[{}]), id="claimed-cases"),
        pytest.param(
            lambda value: value.update(runtime_module_sha256={"private.module": "a" * 64}),
            id="claimed-module-hashes",
        ),
        pytest.param(
            lambda value: value["setup_failure"].update(phase="private_phase"),
            id="phase-not-allowlisted",
        ),
        pytest.param(
            lambda value: value["setup_failure"].update(dataset_kind="private_dataset"),
            id="dataset-kind-mismatch",
        ),
        pytest.param(
            lambda value: value["setup_failure"].update(error_class="PrivateError"),
            id="error-class-not-allowlisted",
        ),
        pytest.param(lambda value: value.update(reason_code="OTHERERROR"), id="reason-mismatch"),
        pytest.param(
            lambda value: value["setup_failure"].update(cause_classes=["OtherError"] * 5),
            id="too-many-causes",
        ),
        pytest.param(
            lambda value: value["setup_failure"].update(cause_classes=["PrivateError"]),
            id="cause-not-allowlisted",
        ),
    ],
)
def test_setup_failure_receipt_rejects_unbound_or_unallowlisted_evidence(mutate: Any) -> None:
    receipt = _setup_failure_payload(
        executor._ChildPhaseFailure(
            "owner_resolution", ExternalComponentUnavailableError("private details")
        )
    )
    mutate(receipt)

    with pytest.raises(CompatibilityError):
        executor._sanitized_child_receipt(json.dumps(receipt))


def test_child_receipt_rejects_unstructured_failed_status() -> None:
    payload = json.loads(_child_receipt())
    payload["probe"] = "ms5a-bounded-profile-v1"
    payload["status"] = "failed"

    with pytest.raises(CompatibilityError, match="child receipt identity is invalid"):
        executor._sanitized_child_receipt(json.dumps(payload))


def test_setup_failure_payload_rejects_unallowlisted_phase() -> None:
    with pytest.raises(CompatibilityError, match="setup phase is not allowlisted"):
        _setup_failure_payload(
            executor._ChildPhaseFailure(
                "private_phase", ExternalComponentUnavailableError("private details")
            )
        )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda payload: payload["cases"].pop(), id="missing-completed-case"),
        pytest.param(
            lambda payload: payload["cases"][0].update(
                status="failed", reason_code="SYNTHETIC_FAILURE"
            ),
            id="failed-case-in-completed-receipt",
        ),
    ],
)
def test_completed_child_receipt_requires_all_cases_to_complete(mutate: Any) -> None:
    payload = json.loads(_child_receipt())
    mutate(payload)

    with pytest.raises(CompatibilityError, match="claims completion without all cases"):
        executor._sanitized_child_receipt(json.dumps(payload))


def test_setup_failure_maps_untrusted_exception_names_to_other_error() -> None:
    private_error_type = type(
        "PrivateCatalogResolutionFailure",
        (Exception,),
        {"__module__": "tenant.private_runtime"},
    )
    private_cause_type = type(
        "InternalMountCredentialsError",
        (Exception,),
        {"__module__": "tenant.private_runtime"},
    )
    private_error = private_error_type("secret /private/catalog.sqlite3")
    private_error.__cause__ = private_cause_type("credential=do-not-report")

    receipt = _setup_failure_payload(executor._ChildPhaseFailure("owner_resolution", private_error))
    sanitized = executor._sanitized_child_receipt(json.dumps(receipt))
    serialized = json.dumps(sanitized)

    assert sanitized["setup_failure"]["error_class"] == "OtherError"
    assert sanitized["setup_failure"]["cause_classes"] == ["OtherError"]
    for secret in (
        "PrivateCatalogResolutionFailure",
        "InternalMountCredentialsError",
        "tenant.private_runtime",
        "/private/catalog.sqlite3",
        "credential=do-not-report",
    ):
        assert secret not in serialized


def test_setup_failure_cause_walk_stops_at_four_and_terminates_cycles() -> None:
    private_error_type = type(
        "PrivateCauseFailure", (Exception,), {"__module__": "tenant.private_runtime"}
    )
    root = ExternalComponentUnavailableError("do-not-report")
    cursor: BaseException = root
    for index in range(7):
        cause = private_error_type(f"private detail {index}")
        cursor.__cause__ = cause
        cursor = cause

    deep_receipt = _setup_failure_payload(executor._ChildPhaseFailure("owner_resolution", root))
    deep_sanitized = executor._sanitized_child_receipt(json.dumps(deep_receipt))
    deep_causes = deep_sanitized["setup_failure"]["cause_classes"]
    assert deep_causes == ["OtherError"] * 4
    assert "private detail" not in json.dumps(deep_sanitized)

    cyclic_root = ExternalComponentUnavailableError("private cycle message")
    first = private_error_type("private first")
    second = private_error_type("private second")
    cyclic_root.__cause__ = first
    first.__cause__ = second
    second.__cause__ = first

    cyclic_receipt = _setup_failure_payload(
        executor._ChildPhaseFailure("owner_resolution", cyclic_root)
    )
    cyclic_sanitized = executor._sanitized_child_receipt(json.dumps(cyclic_receipt))
    cyclic_causes = cyclic_sanitized["setup_failure"]["cause_classes"]
    assert cyclic_causes == ["OtherError", "OtherError"]
    assert "private" not in json.dumps(cyclic_sanitized)


def test_module_attestation_rejects_imports_without_source_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor.importlib, "import_module", lambda _name: SimpleNamespace())

    with pytest.raises(CompatibilityError, match="has no source file"):
        executor._attest_runtime_modules(tmp_path, SimpleNamespace(trusted_runtime_modules=()))


def test_module_attestation_rejects_source_outside_accepted_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "untrusted_runtime_module.py"
    outside.write_text("private = True\n", encoding="utf-8")
    monkeypatch.setattr(
        executor.importlib,
        "import_module",
        lambda _name: SimpleNamespace(__file__=str(outside)),
    )

    with pytest.raises(CompatibilityError, match="outside accepted source"):
        executor._attest_runtime_modules(tmp_path, SimpleNamespace(trusted_runtime_modules=()))


def test_repaired_module_attestation_hashes_both_loaded_candidate_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = {
        "market_data_ingestion.integrity": (
            "modules/market_data_ingestion/src/market_data_ingestion/integrity.py"
        ),
        "market_data_ingestion.storage": (
            "modules/market_data_ingestion/src/market_data_ingestion/storage.py"
        ),
    }
    imported: dict[str, SimpleNamespace] = {}
    expected: dict[str, str] = {}
    for index, (module_name, relative) in enumerate(modules.items()):
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"synthetic-module-{index}\n", encoding="utf-8")
        expected[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
        imported[module_name] = SimpleNamespace(__file__=str(source))
    monkeypatch.setattr(executor, "DIAGNOSTIC_RUNTIME_FILE_SHA256", expected)
    monkeypatch.setattr(executor.importlib, "import_module", lambda name: imported[name])

    observed = executor._attest_repaired_mdi_modules(tmp_path)

    assert observed == {
        module_name: expected[relative] for module_name, relative in modules.items()
    }


def test_repaired_module_attestation_rejects_source_outside_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path.parent / "untrusted_mdi_module.py"
    outside.write_text("synthetic outside source\n", encoding="utf-8")
    imported = SimpleNamespace(__file__=str(outside))
    monkeypatch.setattr(executor.importlib, "import_module", lambda _name: imported)

    with pytest.raises(CompatibilityError, match="outside the candidate"):
        executor._attest_repaired_mdi_modules(tmp_path)


def test_plan_only_main_writes_report_without_consuming_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"status": "plan_only_no_data_loaded_no_claim_consumed", "cases": []}
    monkeypatch.setattr(executor, "plan_only_report", lambda *_args: payload)
    report = tmp_path / "plan.json"

    assert (
        executor.main(
            [
                "--plan-only",
                "--runtime-root",
                str(tmp_path),
                "--runtime-source",
                str(tmp_path),
                "--report",
                str(report),
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == payload
    assert json.loads(report.read_text()) == payload
    assert not (tmp_path / "consumed.json").exists()


def test_profile_execution_entry_points_are_stopped_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        executor, "_require_profile_execution_enabled", _DEFAULT_PROFILE_EXECUTION_GUARD
    )
    side_effects: list[str] = []

    def forbidden(name: str) -> Any:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            side_effects.append(name)
            raise AssertionError(f"stopped entry point reached {name}")

        return fail

    for name in (
        "_consume_claim",
        "_child_command",
        "_load_verified_owner_module",
        "_write_report",
    ):
        monkeypatch.setattr(executor, name, forbidden(name))
    monkeypatch.setattr(executor.Path, "resolve", forbidden("Path.resolve"))

    with pytest.raises(CompatibilityError, match="MS5A_PROFILE_EXECUTION_STOPPED_1190"):
        executor.execute_profile(
            runtime_root=tmp_path / "runtime",
            runtime_source=tmp_path / "source",
            market_data_root=tmp_path / "market-data",
            authority_path=tmp_path / "authority.json",
            claim_root=tmp_path / "claims",
            scratch_root=tmp_path / "scratch",
            report_path=tmp_path / "report.json",
            controller_root=tmp_path / "controller",
        )
    with pytest.raises(CompatibilityError, match="MS5A_PROFILE_EXECUTION_STOPPED_1190"):
        executor._child_main([])
    with pytest.raises(CompatibilityError, match="MS5A_PROFILE_EXECUTION_STOPPED_1190"):
        executor._child_run(
            tmp_path / "source",
            tmp_path / "runtime",
            tmp_path / "market-data",
            tmp_path / "scratch",
            {},
        )
    with pytest.raises(CompatibilityError, match="MS5A_PROFILE_EXECUTION_STOPPED_1190"):
        executor._prefix_input_hash(tmp_path / "market-data", None, None, 1)

    status = executor.main(
        [
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--runtime-source",
            str(tmp_path / "source"),
            "--market-data-root",
            str(tmp_path / "market-data"),
            "--authority-manifest",
            str(tmp_path / "authority.json"),
            "--claim-root",
            str(tmp_path / "claims"),
            "--scratch-root",
            str(tmp_path / "scratch"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    assert status == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "reason_code": "MS5A_PROFILE_EXECUTION_STOPPED_1190",
    }
    assert side_effects == []
    assert list(tmp_path.iterdir()) == []


def test_main_returns_sanitized_nonzero_for_execution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_kwargs: Any) -> Any:
        raise CompatibilityError("private dataset path")

    monkeypatch.setattr(executor, "execute_profile", fail)
    status = executor.main(
        [
            "--runtime-root",
            str(tmp_path),
            "--runtime-source",
            str(tmp_path),
            "--market-data-root",
            str(tmp_path),
            "--authority-manifest",
            str(tmp_path / "authority"),
            "--claim-root",
            str(tmp_path),
            "--scratch-root",
            str(tmp_path),
            "--report",
            str(tmp_path / "report"),
        ]
    )
    assert status == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "reason_code": "COMPATIBILITYERROR",
    }
