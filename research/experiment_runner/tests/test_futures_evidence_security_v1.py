from __future__ import annotations

from pathlib import Path

import pytest

from modular_research_runner import futures_evidence
from modular_research_runner.models import SpecificationError


@pytest.mark.parametrize(
    "value",
    [
        "/private/runner/work/mark.parquet",
        "file:///private/dataset/manifest.json",
        "https://private-exchange.invalid/api",
        r"C:\private\runner\work\mark.parquet",
        r"\\private-host\share\mark.parquet",
    ],
)
def test_public_futures_source_text_rejects_storage_and_endpoint_metadata(value: str) -> None:
    with pytest.raises(SpecificationError, match="private storage metadata"):
        futures_evidence._public_text(value, "producer source")


def test_futures_evidence_read_is_bounded_before_json_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(futures_evidence, "_MAX_EVIDENCE_BYTES", 32)
    evidence = tmp_path / "application-run.json"
    evidence.write_bytes(b"x" * 33)

    with pytest.raises(SpecificationError, match="exceeds safe evidence size"):
        futures_evidence._json_mapping(evidence, "application-run.json")


def test_futures_evidence_read_error_does_not_echo_storage_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "/private/sentinel/runner/work/application-run.json"
    evidence = tmp_path / "application-run.json"
    evidence.write_text("{}", encoding="utf-8")
    original_open = Path.open

    def failing_open(path: Path, *args: object, **kwargs: object):
        if path == evidence:
            raise OSError(f"cannot read {sentinel}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(SpecificationError) as caught:
        futures_evidence._json_mapping(evidence, "application-run.json")

    assert sentinel not in str(caught.value)
    assert str(caught.value) == "Backtest Engine application-run.json cannot be read"
