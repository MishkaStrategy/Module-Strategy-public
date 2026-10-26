from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import pytest
from test_ranking_package import candidate

from strategy_factory.budget import BudgetLedger, _positive, _read_int
from strategy_factory.canonical import (
    canonical_json_bytes,
    freeze,
    strict_json_loads,
)
from strategy_factory.contracts import (
    DecisionAction,
    FactoryDecision,
    FactoryStage,
)
from strategy_factory.decision_log import DecisionLog, _decision_from_mapping
from strategy_factory.errors import (
    BudgetExhaustedError,
    CheckpointError,
    ContractError,
    IdempotencyConflictError,
)
from strategy_factory.idempotency import IdempotencyLedger
from strategy_factory.observability import (
    FactoryMetrics,
    JsonLogFormatter,
    _redact,
    _render_key,
)
from strategy_factory.ranking import (
    Constraint,
    Objective,
    ObjectiveDirection,
    ParetoRanker,
    _decimal,
)


@dataclass(frozen=True)
class _CanonicalDataclass:
    value: int


class _CanonicalEnum(StrEnum):
    VALUE = "value"


def test_canonical_normalizes_supported_types_and_freezes() -> None:
    value = {
        "dataclass": _CanonicalDataclass(1),
        "enum": _CanonicalEnum.VALUE,
        "datetime": datetime(2026, 1, 1, tzinfo=UTC),
        "decimal": Decimal("1.20"),
        "path": Path("a/b"),
        "set": {2, 1},
        "sequence": (1, 2),
        "float": 1.5,
        "none": None,
    }
    decoded = json.loads(canonical_json_bytes(value))
    assert decoded["dataclass"] == {"value": 1}
    assert decoded["enum"] == "value"
    assert decoded["decimal"] == {"$decimal": "1.20"}
    assert decoded["path"] == "a/b"
    assert decoded["set"] == [1, 2]

    source = {"list": [{"value": 1}], "set": {1, 2}}
    frozen = freeze(source)
    source["list"][0]["value"] = 2
    assert frozen["list"][0]["value"] == 1
    with pytest.raises(TypeError):
        frozen["new"] = 1


def test_canonical_rejects_invalid_values_and_json() -> None:
    for invalid, message in (
        (datetime(2026, 1, 1), "timezone-aware"),
        (Decimal("NaN"), "Decimal values must be finite"),
        (float("inf"), "floating-point values must be finite"),
        ({1: "bad"}, "mapping keys"),
        (object(), "unsupported canonical value type"),
    ):
        with pytest.raises(ContractError, match=message):
            canonical_json_bytes(invalid)

    nested: object = 0
    for _ in range(98):
        nested = [nested]
    with pytest.raises(ContractError, match="maximum nesting depth"):
        canonical_json_bytes(nested)

    with pytest.raises(ContractError, match="duplicate JSON key"):
        strict_json_loads('{"a":1,"a":2}')
    with pytest.raises(ContractError, match="non-finite JSON constant"):
        strict_json_loads('{"a":NaN}')
    with pytest.raises(ContractError, match="invalid JSON document"):
        strict_json_loads(b"\xff")


def test_budget_ledger_limits_and_restore_validation(budget) -> None:
    ledger = BudgetLedger(budget)
    with pytest.raises(ContractError, match="positive"):
        ledger.reserve_experiments(FactoryStage.COARSE_SEARCH, 0)
    ledger.reserve_experiments(FactoryStage.COARSE_SEARCH, 40)
    with pytest.raises(BudgetExhaustedError, match="stage budget"):
        ledger.reserve_experiments(FactoryStage.COARSE_SEARCH, 1)

    ledger.record_architectures(20)
    with pytest.raises(BudgetExhaustedError, match="architecture"):
        ledger.record_architectures(1)
    with pytest.raises(BudgetExhaustedError, match="finalist"):
        ledger.record_finalists(4)
    ledger.record_finalists(3)
    assert ledger.snapshot().finalists_selected == 3

    expired = BudgetLedger(budget)
    expired._started_ns -= (budget.maximum_duration_seconds + 1) * 1_000_000_000
    with pytest.raises(BudgetExhaustedError, match="duration"):
        expired.ensure_time_available()

    invalid_states = [
        (
            {
                "experiments_reserved": 0,
                "architectures_generated": 0,
                "finalists_selected": 0,
                "stage_experiments": [],
            },
            "stage_experiments",
        ),
        (
            {
                "experiments_reserved": 1,
                "architectures_generated": 0,
                "finalists_selected": 0,
                "stage_experiments": {"BAD": 1},
            },
            "invalid restored budget stage",
        ),
        (
            {
                "experiments_reserved": 1,
                "architectures_generated": 0,
                "finalists_selected": 0,
                "stage_experiments": {FactoryStage.COARSE_SEARCH.value: True},
            },
            "invalid restored stage budget",
        ),
        (
            {
                "experiments_reserved": 101,
                "architectures_generated": 0,
                "finalists_selected": 0,
                "stage_experiments": {FactoryStage.COARSE_SEARCH.value: 101},
            },
            "experiment budget exceeds",
        ),
        (
            {
                "experiments_reserved": 0,
                "architectures_generated": 21,
                "finalists_selected": 0,
                "stage_experiments": {},
            },
            "architecture budget exceeds",
        ),
        (
            {
                "experiments_reserved": 0,
                "architectures_generated": 0,
                "finalists_selected": 4,
                "stage_experiments": {},
            },
            "finalist budget exceeds",
        ),
        (
            {
                "experiments_reserved": 41,
                "architectures_generated": 0,
                "finalists_selected": 0,
                "stage_experiments": {FactoryStage.COARSE_SEARCH.value: 41},
            },
            "stage budget exceeds",
        ),
    ]
    for state, message in invalid_states:
        with pytest.raises(ContractError, match=message):
            BudgetLedger(budget).restore(state)

    restored = BudgetLedger(budget)
    restored.restore(
        {
            "experiments_reserved": 2,
            "architectures_generated": 1,
            "finalists_selected": 1,
            "stage_experiments": {FactoryStage.COARSE_SEARCH.value: 2},
        }
    )
    assert restored.snapshot().experiments_reserved == 2


def test_budget_scalar_validators() -> None:
    for value in (0, -1, True, "1"):
        with pytest.raises(ContractError):
            _positive(value, "count")  # type: ignore[arg-type]
    _positive(0, "count", allow_zero=True)
    for value in (-1, True, "1", None):
        with pytest.raises(ContractError, match="invalid restored budget counter"):
            _read_int({"value": value}, "value")


def test_idempotency_result_binding_and_unclaimed_rejection(tmp_path: Path) -> None:
    ledger = IdempotencyLedger(tmp_path / "ledger.sqlite3")
    try:
        assert ledger.result(scope="run", operation_id="missing") is None
        with pytest.raises(IdempotencyConflictError, match="unclaimed"):
            ledger.bind_result(scope="run", operation_id="missing", result_reference="result-1")
        assert ledger.claim(scope="run", operation_id="one", content_hash="a" * 64)
        ledger.bind_result(scope="run", operation_id="one", result_reference="result-1")
        assert ledger.result(scope="run", operation_id="one") == "result-1"
    finally:
        ledger.close()


def _decision(decision_id: str, *, subject_id: str = "subject") -> FactoryDecision:
    return FactoryDecision(
        decision_id=decision_id,
        run_id="run-decision",
        stage=FactoryStage.COARSE_SEARCH,
        action=DecisionAction.PROMOTE,
        subject_id=subject_id,
        reason_codes=("PASSED",),
        evidence={"metric": "1.2"},
        alternatives=("reject",),
        budget_snapshot={"experiments": 1},
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_decision_log_idempotency_chain_and_corruption(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "decisions.jsonl"
    log = DecisionLog(path)
    assert log.read_all() == ()
    assert log.chain_head is None
    first = log.append(_decision("decision-one"))
    assert log.append(_decision("decision-one")) == first
    with pytest.raises(ContractError, match="different content"):
        log.append(_decision("decision-one", subject_id="other"))
    second = log.append(_decision("decision-two"))
    assert second.previous_hash == first.entry_hash
    assert log.chain_head == second.entry_hash

    lines = path.read_text(encoding="utf-8").splitlines()
    value = json.loads(lines[1])
    value["sequence"] = 9
    lines[1] = json.dumps(value)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="chain ordering"):
        log.read_all()

    original_read = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(OSError()) if self == path else original_read(self),
    )
    with pytest.raises(CheckpointError, match="cannot read decision log"):
        log.read_all()


def test_decision_log_rejects_entry_and_payload_corruption(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    log = DecisionLog(path)
    log.append(_decision("decision-one"))
    original = json.loads(path.read_text(encoding="utf-8"))

    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="entry must be an object"):
        log.read_all()

    original["entry_hash"] = "a" * 64
    path.write_text(json.dumps(original) + "\n", encoding="utf-8")
    with pytest.raises(CheckpointError, match="hash mismatch"):
        log.read_all()

    for payload in (None, {}, {"decision_id": "only"}):
        with pytest.raises(CheckpointError, match="invalid decision payload"):
            _decision_from_mapping(payload)


def test_observability_validates_logs_metrics_labels_and_redaction() -> None:
    with pytest.raises(ContractError, match="at least 64"):
        JsonLogFormatter(maximum_field_length=63)

    formatter = JsonLogFormatter(maximum_field_length=64)
    try:
        raise ValueError("x" * 100)
    except ValueError:
        record = logging.LogRecord(
            "factory",
            logging.ERROR,
            __file__,
            1,
            "m" * 100,
            (),
            sys.exc_info(),
        )
    for field in ("correlation_id", "experiment_id", "stage", "candidate_id", "event_id"):
        setattr(record, field, field)
    rendered = json.loads(formatter.format(record))
    assert rendered["message"].endswith("[truncated]")
    assert rendered["exception_type"] == "ValueError"
    assert rendered["exception_message"].endswith("[truncated]")

    metrics = FactoryMetrics()
    invalid = [
        ("bad name", Decimal("1"), None, "invalid metric name"),
        ("good_name", Decimal("NaN"), None, "finite Decimal"),
        ("good_name", 1, None, "finite Decimal"),
        ("good_name", Decimal("1"), {f"l{i}": "v" for i in range(9)}, "too many"),
        ("good_name", Decimal("1"), {"bad label": "v"}, "invalid metric label"),
        ("good_name", Decimal("1"), {"label": "x" * 129}, "invalid metric label"),
    ]
    for name, value, labels, message in invalid:
        with pytest.raises(ContractError, match=message):
            metrics.increment(name, value, labels=labels)  # type: ignore[arg-type]
    metrics.set_gauge("metric", Decimal("1"))
    with pytest.raises(ContractError, match="type conflict"):
        metrics.increment("metric")
    assert FactoryMetrics().render_prometheus() == ""
    assert _render_key("metric", ()) == "metric"
    assert _render_key("metric", (("label", 'a\\"b'),)) == 'metric{label="a\\\\\\"b"}'
    assert _redact({"password": "secret"}, 64) == {"password": "[REDACTED]"}
    deep: object = "value"
    for _ in range(14):
        deep = {"safe": deep}
    assert "maximum-depth" in str(_redact(deep, 64))
    assert len(_redact(list(range(120)), 64)) == 100


def test_ranking_contracts_and_rejection_reasons(architecture) -> None:
    with pytest.raises(ContractError, match="metric cannot be empty"):
        Objective("", ObjectiveDirection.MAXIMIZE)
    with pytest.raises(ContractError, match="epsilon"):
        Objective("return", ObjectiveDirection.MAXIMIZE, Decimal("-1"))
    with pytest.raises(ContractError, match="metric and bound"):
        Constraint("")
    with pytest.raises(ContractError, match="minimum exceeds"):
        Constraint("trades", minimum=Decimal("2"), maximum=Decimal("1"))
    with pytest.raises(ContractError, match="at least one objective"):
        ParetoRanker(objectives=(), constraints=())
    with pytest.raises(ContractError, match="unique"):
        ParetoRanker(
            objectives=(
                Objective("return", ObjectiveDirection.MAXIMIZE),
                Objective("return", ObjectiveDirection.MINIMIZE),
            ),
            constraints=(),
        )

    missing_evaluation = replace(
        candidate(architecture, "missing-eval", {"return": 1}, "1"),
        evaluation=None,
    )
    unstable = candidate(architecture, "unstable", {"return": 1, "trades": 5}, "1")
    unstable = replace(
        unstable,
        evaluation=replace(unstable.evaluation, instability_flags=("flag",)),
    )
    result = ParetoRanker(
        objectives=(Objective("return", ObjectiveDirection.MAXIMIZE),),
        constraints=(Constraint("trades", minimum=Decimal("10")),),
    ).rank((missing_evaluation, unstable))
    reasons = {item.candidate.candidate_id: item.selection_reasons for item in result.rejected}
    assert reasons["missing-eval"] == ("MISSING_EVALUATION",)
    assert "CONSTRAINT_FAILURE" in reasons["unstable"]
    assert "INSTABILITY_FLAGS_PRESENT" in reasons["unstable"]


def test_ranking_decimal_and_epsilon_dominance(architecture) -> None:
    assert _decimal(None) is None
    assert _decimal(True) is None
    assert _decimal("bad") is None
    assert _decimal("NaN") is None
    assert _decimal("1.2") == Decimal("1.2")

    left = candidate(architecture, "left", {"return": "1.00", "drawdown": "0.20"}, "1")
    right = candidate(architecture, "right", {"return": "1.05", "drawdown": "0.10"}, "2")
    ranker = ParetoRanker(
        objectives=(
            Objective("return", ObjectiveDirection.MAXIMIZE, Decimal("0.1")),
            Objective("drawdown", ObjectiveDirection.MINIMIZE),
        ),
        constraints=(),
    )
    result = ranker.rank((left, right))
    assert result.ranked[0].candidate.candidate_id == "right"
