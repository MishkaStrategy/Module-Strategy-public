from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_application.download_config_migration import (
    migrate_legacy_download_config,
    serialize_download_request,
    write_download_request,
)
from market_data_application.errors import ApplicationError
from market_data_application.migration_cli import main
from market_data_ingestion.enums import DataType, MarketType


def write_config(path: Path, *, overrides: str = "") -> Path:
    path.write_text(
        """schema_version: 1
exchange:
  id: fake
  market_type: usdt_m_perpetual
instrument:
  symbol: btcusdt
range:
  mode: explicit
  start: "2026-01-01T00:00:00Z"
  end: "2026-01-01T01:00:00Z"
data:
  base_timeframe: 5m
  derived_timeframes: [15m]
  required_types: [klines]
  optional_types: [funding_rate]
download:
  concurrency: 3
  resume: true
  max_records_in_memory: 1234
validation:
  reject_unresolved_required_gaps: false
storage:
  root: ./data
  format: csv
  compression: gzip
  partition: monthly
"""
        + overrides,
        encoding="utf-8",
    )
    return path


def test_migration_preserves_representable_download_contract(tmp_path, monkeypatch):
    source = write_config(tmp_path / "legacy.yaml")

    def forbidden_execute(*args, **kwargs):
        raise AssertionError("migration must not execute acquisition")

    monkeypatch.setattr(
        "market_data_ingestion.pipeline.IngestionService.execute",
        forbidden_execute,
    )
    request = migrate_legacy_download_config(source)

    assert request.exchange_id == "fake"
    assert request.market_type is MarketType.USDT_M_PERPETUAL
    assert request.symbol == "BTCUSDT"
    assert request.required_data == (DataType.KLINES,)
    assert request.optional_data == (DataType.FUNDING_RATE,)
    assert request.base_timeframe == "5m"
    assert request.derived_timeframes == ("15m",)
    assert request.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert request.end == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert request.full_available_history is False
    assert request.allow_provisional is False
    assert request.storage_format.value == "csv"
    assert request.compression == "gzip"
    assert request.reject_unresolved_required_gaps is False
    assert request.concurrency == 3
    assert request.max_records_in_memory == 1234
    assert sorted(tmp_path.iterdir()) == [source]


def test_latest_closed_requires_explicit_as_of_and_is_deterministic(tmp_path):
    source = write_config(tmp_path / "latest.yaml")
    content = source.read_text(encoding="utf-8").replace(
        '  end: "2026-01-01T01:00:00Z"',
        "  end: latest_closed",
    )
    source.write_text(content, encoding="utf-8")

    with pytest.raises(ApplicationError) as missing:
        migrate_legacy_download_config(source)
    assert missing.value.code == "LEGACY_CONFIG_DYNAMIC_END_REQUIRES_AS_OF"

    as_of = datetime(2026, 1, 1, 1, 7, 59, tzinfo=UTC)
    first = migrate_legacy_download_config(source, as_of=as_of)
    second = migrate_legacy_download_config(source, as_of=as_of)
    assert first.end == datetime(2026, 1, 1, 1, 5, tzinfo=UTC)
    assert serialize_download_request(first) == serialize_download_request(second)


def test_full_available_history_is_preserved_with_deterministic_end(tmp_path):
    source = write_config(tmp_path / "full.yaml")
    content = source.read_text(encoding="utf-8")
    content = content.replace("  mode: explicit", "  mode: full_available_history")
    content = content.replace('  start: "2026-01-01T00:00:00Z"\n', "")
    content = content.replace('  end: "2026-01-01T01:00:00Z"', "  end: latest_closed")
    source.write_text(content, encoding="utf-8")

    request = migrate_legacy_download_config(
        source,
        as_of=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
    )
    assert request.full_available_history is True
    assert request.start is None
    assert request.end == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("fragment", "code"),
    [
        ("request_id: explicit-run\n", "LEGACY_CONFIG_REQUEST_ID_UNREPRESENTABLE"),
        (
            "download:\n  concurrency: 3\n  resume: false\n  max_records_in_memory: 1234\n",
            "LEGACY_CONFIG_RESUME_UNREPRESENTABLE",
        ),
        (
            "download:\n  concurrency: 3\n  resume: true\n  retries: 9\n  max_records_in_memory: 1234\n",
            "LEGACY_CONFIG_RUNTIME_SETTINGS_UNREPRESENTABLE",
        ),
        (
            "validation:\n  reject_unresolved_required_gaps: false\n  deep: true\n",
            "LEGACY_CONFIG_VALIDATION_SETTINGS_UNREPRESENTABLE",
        ),
        (
            "storage:\n  root: ../elsewhere\n  format: csv\n  compression: gzip\n  partition: monthly\n",
            "LEGACY_CONFIG_STORAGE_ROOT_UNREPRESENTABLE",
        ),
    ],
)
def test_unrepresentable_explicit_values_fail_closed(tmp_path, fragment, code):
    source = write_config(tmp_path / f"{code}.yaml")
    text = source.read_text(encoding="utf-8")
    section = fragment.split(":", 1)[0]
    if section in {"download", "validation", "storage"}:
        start = text.index(f"{section}:\n")
        next_sections = [
            text.find(f"{name}:\n", start + 1)
            for name in ("validation", "storage")
            if text.find(f"{name}:\n", start + 1) >= 0
        ]
        end = min(next_sections) if next_sections else len(text)
        text = text[:start] + fragment + text[end:]
    else:
        text += fragment
    source.write_text(text, encoding="utf-8")

    with pytest.raises(ApplicationError) as rejected:
        migrate_legacy_download_config(source)
    assert rejected.value.code == code


def test_secret_like_fields_are_rejected_without_value_leakage(tmp_path):
    source = write_config(tmp_path / "secret.yaml")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "  market_type: usdt_m_perpetual",
            "  market_type: usdt_m_perpetual\n  api_key: never-print-this-value",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ApplicationError) as rejected:
        migrate_legacy_download_config(source)
    assert rejected.value.code == "LEGACY_CONFIG_SECRET_FIELD_FORBIDDEN"
    assert rejected.value.details == {"field_paths": ["exchange.api_key"]}
    assert "never-print-this-value" not in str(rejected.value.details)


def test_source_symlink_and_oversized_source_are_rejected(tmp_path):
    source = write_config(tmp_path / "source.yaml")
    symlink = tmp_path / "linked.yaml"
    symlink.symlink_to(source)
    with pytest.raises(ApplicationError) as linked:
        migrate_legacy_download_config(symlink)
    assert linked.value.code == "LEGACY_CONFIG_SOURCE_UNSAFE"

    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ApplicationError) as large:
        migrate_legacy_download_config(oversized)
    assert large.value.code == "LEGACY_CONFIG_SOURCE_TOO_LARGE"


def test_atomic_output_is_deterministic_private_and_no_clobber(tmp_path):
    request = migrate_legacy_download_config(write_config(tmp_path / "source.yaml"))
    output = tmp_path / "request.json"

    published = write_download_request(output, request)
    assert published == output
    assert output.read_text(encoding="utf-8") == serialize_download_request(request)
    assert stat_mode(output) == 0o600

    with pytest.raises(ApplicationError) as duplicate:
        write_download_request(output, request)
    assert duplicate.value.code == "MIGRATION_OUTPUT_EXISTS"
    assert not list(tmp_path.glob(".market-data-v1-migrate-*.tmp"))


def test_output_parent_symlink_is_rejected(tmp_path):
    request = migrate_legacy_download_config(write_config(tmp_path / "source.yaml"))
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ApplicationError) as unsafe:
        write_download_request(linked / "request.json", request)
    assert unsafe.value.code == "MIGRATION_OUTPUT_UNSAFE"
    assert not (real / "request.json").exists()


def test_cli_stdout_is_strict_request_json_and_optional_file_matches(tmp_path, capsys):
    source = write_config(tmp_path / "legacy.yaml")
    output = tmp_path / "migrated.json"

    exit_code = main(["--config", str(source), "--output", str(output)])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["exchange_id"] == "fake"
    assert "status" not in payload
    assert output.read_text(encoding="utf-8") == captured.out


def test_cli_structured_error_exit_codes(tmp_path, capsys):
    source = write_config(tmp_path / "latest.yaml")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            '  end: "2026-01-01T01:00:00Z"',
            "  end: latest_closed",
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(source)])
    response = json.loads(capsys.readouterr().out)
    assert exit_code == 7
    assert response["error"]["code"] == "LEGACY_CONFIG_DYNAMIC_END_REQUIRES_AS_OF"
    assert response["error"]["correlation_id"].startswith("corr_migrate_")


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
