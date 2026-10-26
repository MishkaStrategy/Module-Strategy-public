from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategy_factory.adapters import ResearchRunnerExecutionAdapter
from strategy_factory.contracts import (
    DatasetGateStatus,
    DatasetValidationReport,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.results_publication import (
    PublishedRegistryRun,
    ResearchResultsPublicationAdapter,
    ResultsPublicationPolicy,
    _core_metadata,
    _digest,
    _text,
    _utc,
)


class _FakeExecution(ResearchRunnerExecutionAdapter):
    def __init__(
        self,
        *,
        snapshot: object | None = None,
        lineage: dict[str, object] | None = None,
        references: tuple[object, ...] = (),
    ) -> None:
        self.snapshot = snapshot or SimpleNamespace(
            has_nonterminal_runs=False,
            status="completed",
        )
        self.lineage = {"dependency_lock_hash": "d" * 64} if lineage is None else lineage
        self.references = references

    def status(self, experiment_id: str):
        return self.snapshot

    def publication_lineage(self, experiment_id: str):
        return self.lineage

    def publish_registry_packages(self, experiment_id: str, *, sink, created_at):
        return self.references


def _dataset_report(factory_request, **changes: object) -> DatasetValidationReport:
    values: dict[str, object] = {
        "report_id": "dataset-report-edge",
        "dataset_id": factory_request.dataset.dataset_id,
        "dataset_fingerprint": factory_request.dataset.fingerprint,
        "status": DatasetGateStatus.ACCEPTED,
        "checked_at": datetime(2026, 1, 3, tzinfo=UTC),
        "rows_scanned": factory_request.dataset.row_count,
        "duplicates": 0,
        "gaps": 0,
        "out_of_order": 0,
        "invalid_ohlc": 0,
        "invalid_volume": 0,
        "extreme_outliers": 0,
        "computed_sha256": factory_request.dataset.sha256,
        "reason_codes": (),
        "warnings": (),
        "evidence": {},
    }
    values.update(changes)
    return DatasetValidationReport(**values)  # type: ignore[arg-type]


def _adapter(factory_request, tmp_path: Path, execution=None, dataset_report=None):
    return ResearchResultsPublicationAdapter(
        execution=execution or _FakeExecution(),
        request=factory_request,
        package_root=tmp_path / "packages",
        registry_path=tmp_path / "registry.sqlite3",
        producer_module_version="0.3.0",
        dataset_report=dataset_report,
    )


@pytest.mark.parametrize(
    "field_name",
    ["worker_id", "machine_id", "preprocessing_version", "aggregation_version"],
)
def test_publication_policy_rejects_blank_identity(field_name: str) -> None:
    with pytest.raises(CompatibilityError, match=field_name):
        ResultsPublicationPolicy(**{field_name: " "})


@pytest.mark.parametrize(
    "field_name",
    ["execution_assumptions", "risk_settings", "cost_settings", "funding_settings"],
)
def test_publication_policy_rejects_non_mapping_and_freezes(field_name: str) -> None:
    with pytest.raises(CompatibilityError, match=field_name):
        ResultsPublicationPolicy(**{field_name: []})
    source = {"value": 1}
    policy = ResultsPublicationPolicy(**{field_name: source})
    source["value"] = 2
    assert getattr(policy, field_name)["value"] == 1
    with pytest.raises(TypeError):
        getattr(policy, field_name)["value"] = 3


def _published(**changes: object) -> PublishedRegistryRun:
    values: dict[str, object] = {
        "research_package_id": "research-package",
        "registration_package_id": "registration-package",
        "experiment_id": "experiment-edge",
        "run_id": "run-edge",
        "configuration_id": "configuration-edge",
        "native_configuration_hash": "a" * 64,
        "factory_configuration_hash": "b" * 64,
        "validation_hash": "c" * 64,
        "registry_state": "REGISTERED",
        "registry_revision": 1,
        "registered_entities": 5,
        "duplicate_entities": 0,
    }
    values.update(changes)
    return PublishedRegistryRun(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field_name",
    [
        "research_package_id",
        "registration_package_id",
        "experiment_id",
        "run_id",
        "configuration_id",
        "registry_state",
    ],
)
def test_published_registry_run_rejects_blank_identity(field_name: str) -> None:
    with pytest.raises(CompatibilityError, match=field_name):
        _published(**{field_name: ""})


@pytest.mark.parametrize(
    "field_name",
    [
        "native_configuration_hash",
        "factory_configuration_hash",
        "validation_hash",
    ],
)
def test_published_registry_run_rejects_bad_hash(field_name: str) -> None:
    with pytest.raises(CompatibilityError, match=field_name):
        _published(**{field_name: "BAD"})


@pytest.mark.parametrize(
    "field_name",
    ["registry_revision", "registered_entities", "duplicate_entities"],
)
@pytest.mark.parametrize("value", [-1, True, "1"])
def test_published_registry_run_rejects_bad_counts(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(CompatibilityError, match=field_name):
        _published(**{field_name: value})


def test_published_registry_run_rejects_unknown_state() -> None:
    with pytest.raises(CompatibilityError, match="REGISTERED or DUPLICATE"):
        _published(registry_state="FAILED")
    assert _published(registry_state="DUPLICATE").registry_state == "DUPLICATE"


def test_adapter_init_rejects_wrong_types_and_blank_version(
    factory_request, tmp_path: Path
) -> None:
    with pytest.raises(TypeError, match="execution"):
        ResearchResultsPublicationAdapter(
            execution=object(),  # type: ignore[arg-type]
            request=factory_request,
            package_root=tmp_path / "packages-a",
            registry_path=tmp_path / "a.sqlite3",
            producer_module_version="0.3.0",
        )
    with pytest.raises(TypeError, match="request"):
        ResearchResultsPublicationAdapter(
            execution=_FakeExecution(),
            request=object(),  # type: ignore[arg-type]
            package_root=tmp_path / "packages-b",
            registry_path=tmp_path / "b.sqlite3",
            producer_module_version="0.3.0",
        )
    with pytest.raises(CompatibilityError, match="producer_module_version"):
        ResearchResultsPublicationAdapter(
            execution=_FakeExecution(),
            request=factory_request,
            package_root=tmp_path / "packages-c",
            registry_path=tmp_path / "c.sqlite3",
            producer_module_version=" ",
        )


def test_adapter_init_rejects_invalid_dataset_reports(factory_request, tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="DatasetValidationReport"):
        _adapter(factory_request, tmp_path / "type", dataset_report=object())
    with pytest.raises(CompatibilityError, match="another dataset"):
        _adapter(
            factory_request,
            tmp_path / "id",
            dataset_report=_dataset_report(factory_request, dataset_id="another-dataset"),
        )
    with pytest.raises(CompatibilityError, match="fingerprint"):
        _adapter(
            factory_request,
            tmp_path / "fingerprint",
            dataset_report=_dataset_report(
                factory_request,
                dataset_fingerprint="f" * 64,
            ),
        )
    with pytest.raises(CompatibilityError, match="hash does not match"):
        _adapter(
            factory_request,
            tmp_path / "hash",
            dataset_report=_dataset_report(factory_request, computed_sha256="e" * 64),
        )
    rejected = _dataset_report(
        factory_request,
        status=DatasetGateStatus.REJECTED,
        reason_codes=("INVALID",),
    )
    with pytest.raises(CompatibilityError, match="rejected dataset"):
        _adapter(factory_request, tmp_path / "rejected", dataset_report=rejected)


def test_dataset_record_preserves_gate_evidence(factory_request, tmp_path: Path) -> None:
    from modular_results_registry import DatasetRecord

    report = _dataset_report(
        factory_request,
        status=DatasetGateStatus.ACCEPTED_WITH_WARNINGS,
        gaps=2,
        duplicates=3,
        out_of_order=1,
        reason_codes=("GAPS",),
        warnings=("partial history",),
    )
    adapter = _adapter(factory_request, tmp_path, dataset_report=report)
    record = adapter._dataset_record(DatasetRecord)
    assert record.quality_status == DatasetGateStatus.ACCEPTED_WITH_WARNINGS.value
    assert record.gap_summary["gaps"] == 2
    assert record.gap_summary["out_of_order"] == 1
    assert record.duplicate_summary["duplicates"] == 3
    adapter.close()


def test_publish_experiment_rejects_invalid_inputs_and_overlap(
    factory_request, tmp_path: Path
) -> None:
    adapter = _adapter(factory_request, tmp_path)
    with pytest.raises(CompatibilityError, match="experiment_id"):
        adapter.publish_experiment("", observed_at=datetime.now(UTC))
    with pytest.raises(CompatibilityError, match="timezone-aware"):
        adapter.publish_experiment("experiment", observed_at=datetime(2026, 1, 1))
    with pytest.raises(CompatibilityError, match="lowercase SHA-256"):
        adapter.publish_experiment(
            "experiment",
            observed_at=datetime.now(UTC),
            baseline_native_hashes=frozenset({"BAD"}),
        )
    digest = "a" * 64
    with pytest.raises(CompatibilityError, match="both baseline and finalist"):
        adapter.publish_experiment(
            "experiment",
            observed_at=datetime.now(UTC),
            baseline_native_hashes=frozenset({digest}),
            finalist_native_hashes=frozenset({digest}),
        )
    adapter.close()


@pytest.mark.parametrize(
    "snapshot",
    [
        SimpleNamespace(has_nonterminal_runs=True, status="completed"),
        SimpleNamespace(has_nonterminal_runs=False, status="failed"),
    ],
)
def test_publish_experiment_requires_terminal_success(
    factory_request, tmp_path: Path, snapshot: object
) -> None:
    adapter = _adapter(
        factory_request,
        tmp_path,
        execution=_FakeExecution(snapshot=snapshot),
    )
    with pytest.raises(CompatibilityError, match="terminal successful"):
        adapter.publish_experiment("experiment", observed_at=datetime.now(UTC))
    adapter.close()


@pytest.mark.parametrize(
    "lineage",
    [
        {},
        {"dependency_lock_hash": None},
        {"dependency_lock_hash": "BAD"},
    ],
)
def test_publish_experiment_requires_valid_dependency_lock(
    factory_request, tmp_path: Path, lineage: dict[str, object]
) -> None:
    adapter = _adapter(
        factory_request,
        tmp_path,
        execution=_FakeExecution(lineage=lineage),
    )
    with pytest.raises(CompatibilityError, match="dependency[ _]lock"):
        adapter.publish_experiment("experiment", observed_at=datetime.now(UTC))
    adapter.close()


def test_publish_experiment_rejects_empty_packages(factory_request, tmp_path: Path) -> None:
    adapter = _adapter(factory_request, tmp_path, execution=_FakeExecution(references=()))
    with pytest.raises(CompatibilityError, match="no successful packages"):
        adapter.publish_experiment("experiment", observed_at=datetime.now(UTC))
    adapter.close()


def test_publish_experiment_sorts_references_and_rejects_duplicate_run_ids(
    factory_request, tmp_path: Path, monkeypatch
) -> None:
    references = (
        SimpleNamespace(package_id="package-b"),
        SimpleNamespace(package_id="package-a"),
    )
    adapter = _adapter(
        factory_request,
        tmp_path,
        execution=_FakeExecution(references=references),
    )
    observed: list[str] = []

    def fake_register(reference, **kwargs):
        observed.append(reference.package_id)
        return _published(
            research_package_id=reference.package_id,
            registration_package_id=f"registration-{reference.package_id}",
            run_id="same-run",
        )

    monkeypatch.setattr(adapter, "_register_reference", fake_register)
    with pytest.raises(CompatibilityError, match="duplicate run identities"):
        adapter.publish_experiment("experiment", observed_at=datetime.now(UTC))
    assert observed == ["package-a", "package-b"]
    adapter.close()


def test_candidate_roles_and_reason_rendering() -> None:
    from modular_results_registry import CandidateRole

    baseline = "a" * 64
    finalist = "b" * 64
    assert (
        ResearchResultsPublicationAdapter._candidate_role(
            CandidateRole,
            native_hash=baseline,
            stage_role="train",
            baseline_native_hashes=frozenset({baseline}),
            finalist_native_hashes=frozenset(),
        )
        is CandidateRole.BASELINE
    )
    assert (
        ResearchResultsPublicationAdapter._candidate_role(
            CandidateRole,
            native_hash=finalist,
            stage_role="train",
            baseline_native_hashes=frozenset(),
            finalist_native_hashes=frozenset({finalist}),
        )
        is CandidateRole.FINALIST
    )
    assert (
        ResearchResultsPublicationAdapter._candidate_role(
            CandidateRole,
            native_hash="c" * 64,
            stage_role="final_untouched",
            baseline_native_hashes=frozenset(),
            finalist_native_hashes=frozenset(),
        )
        is CandidateRole.FINALIST
    )
    candidate_role = ResearchResultsPublicationAdapter._candidate_role(
        CandidateRole,
        native_hash="c" * 64,
        stage_role="train",
        baseline_native_hashes=frozenset(),
        finalist_native_hashes=frozenset(),
    )
    assert candidate_role is CandidateRole.CANDIDATE
    assert (
        ResearchResultsPublicationAdapter._role_reason(candidate_role, "train")
        == "STRATEGY_FACTORY_CANDIDATE_TRAIN"
    )


def _write_run(path: Path, document: object) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.json").write_text(json.dumps(document), encoding="utf-8")


def test_core_metadata_reads_versions_and_rejects_bad_documents(tmp_path: Path) -> None:
    valid = tmp_path / "valid"
    _write_run(
        valid,
        {
            "manifest": {
                "kernel_version": "0.4.2",
                "module_manifests": [
                    {"name": "signal", "version": "1.0.0"},
                ],
            }
        },
    )
    metadata = _core_metadata(valid)
    assert metadata["kernel_version"] == "0.4.2"
    assert metadata["module_versions"] == {"core": "0.4.2", "signal": "1.0.0"}

    invalid_json = tmp_path / "invalid-json"
    invalid_json.mkdir()
    (invalid_json / "run.json").write_text("{", encoding="utf-8")
    with pytest.raises(CompatibilityError, match="cannot be decoded"):
        _core_metadata(invalid_json)

    for name, document, message in (
        ("not-mapping", [], "must be a mapping"),
        ("missing-manifest", {}, "manifest is missing"),
        (
            "modules-not-list",
            {"manifest": {"kernel_version": "0.4.2", "module_manifests": {}}},
            "must be a list",
        ),
        (
            "module-not-mapping",
            {"manifest": {"kernel_version": "0.4.2", "module_manifests": [1]}},
            "must be a mapping",
        ),
        (
            "duplicate-core",
            {
                "manifest": {
                    "kernel_version": "0.4.2",
                    "module_manifests": [{"name": "core", "version": "0.4.2"}],
                }
            },
            "duplicate Core module manifest",
        ),
    ):
        directory = tmp_path / name
        _write_run(directory, document)
        with pytest.raises(CompatibilityError, match=message):
            _core_metadata(directory)


def test_publication_scalar_validators() -> None:
    assert _text("value", "name") == "value"
    assert _digest("a" * 64, "digest") == "a" * 64
    aware = datetime(2026, 1, 1, tzinfo=UTC)
    assert _utc(aware, "time") == aware
    with pytest.raises(CompatibilityError, match="non-blank"):
        _text(" ", "name")
    with pytest.raises(CompatibilityError, match="lowercase SHA-256"):
        _digest("A" * 64, "digest")
    with pytest.raises(CompatibilityError, match="timezone-aware"):
        _utc(datetime(2026, 1, 1), "time")
