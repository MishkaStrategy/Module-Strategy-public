from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest

from strategy_factory.errors import ContractError
from strategy_factory.observability import FactoryMetrics, JsonLogFormatter


def test_json_logging_redacts_secrets_and_bounds_fields() -> None:
    formatter = JsonLogFormatter(maximum_field_length=64)
    record = logging.LogRecord("factory", logging.INFO, __file__, 1, "hello", (), None)
    record.run_id = "sf-run-1"
    record.event = {"api_key": "secret-value", "payload": "x" * 100}
    output = json.loads(formatter.format(record))
    assert output["event"]["api_key"] == "[REDACTED]"
    assert output["event"]["payload"].endswith("[truncated]")


def test_prometheus_metrics_are_deterministic() -> None:
    metrics = FactoryMetrics()
    metrics.increment("strategy_factory_events_total", labels={"stage": "data_gate"})
    metrics.increment("strategy_factory_events_total", Decimal("2"), labels={"stage": "data_gate"})
    metrics.set_gauge("strategy_factory_candidates", Decimal("7"))
    rendered = metrics.render_prometheus()
    assert 'strategy_factory_events_total{stage="data_gate"} 3' in rendered
    assert "strategy_factory_candidates 7" in rendered
    assert metrics.snapshot()["strategy_factory_candidates"] == "7"


def test_metric_type_conflict_rejected() -> None:
    metrics = FactoryMetrics()
    metrics.increment("strategy_factory_value")
    with pytest.raises(ContractError, match="type conflict"):
        metrics.set_gauge("strategy_factory_value", Decimal("1"))
