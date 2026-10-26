from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.write_physical_historical_failure_evidence import (
    _workflow_error_annotation,
    publish_failure_evidence,
)


HEAD = "1" * 40
BASE = "2" * 40


def test_publish_failure_evidence_is_bounded_closed_and_path_free(tmp_path: Path) -> None:
    output = tmp_path / "publish"

    payload = publish_failure_evidence(
        output_dir=output,
        candidate_sha=HEAD,
        full_rc_sha=BASE,
        stage="canonical_full",
        exit_code=1,
        run_id="31932058743",
        run_attempt="1",
    )

    assert payload["status"] == "FAIL"
    assert payload["failure_category"] == "canonical_suite"
    assert payload["paper_only"] is True
    assert payload["live_trading_authorized"] is False
    assert payload["raw_helper_log_uploaded"] is False
    assert payload["raw_market_data_uploaded"] is False
    assert {path.name for path in output.iterdir()} == {"failure.json", "SHA256SUMS.txt"}

    failure_bytes = (output / "failure.json").read_bytes()
    assert len(failure_bytes) < 4096
    failure_text = failure_bytes.decode("utf-8")
    assert str(tmp_path) not in failure_text
    assert str(Path.home()) not in failure_text
    parsed = json.loads(failure_text)
    assert parsed["run_id"] == 31932058743
    assert parsed["run_attempt"] == 1

    expected = hashlib.sha256(failure_bytes).hexdigest()
    assert (output / "SHA256SUMS.txt").read_text(encoding="utf-8") == f"{expected}  failure.json\n"


def test_failure_annotation_is_bounded_allowlisted_and_artifact_independent(tmp_path: Path) -> None:
    payload = publish_failure_evidence(
        output_dir=tmp_path / "publish",
        candidate_sha=HEAD,
        full_rc_sha=BASE,
        stage="canonical_full",
        exit_code=1,
        run_id="31942524268",
        run_attempt="2",
    )

    annotation = _workflow_error_annotation(payload)

    assert annotation.startswith("::error title=Physical historical acceptance failure::")
    assert "stage=canonical_full" in annotation
    assert "failure_category=canonical_suite" in annotation
    assert "exit_code=1" in annotation
    assert f"candidate_sha={HEAD}" in annotation
    assert f"full_rc_sha={BASE}" in annotation
    assert "run_id=31942524268" in annotation
    assert "run_attempt=2" in annotation
    assert "paper_only=true" in annotation
    assert "live_trading_authorized=false" in annotation
    assert "raw_helper_log_uploaded=false" in annotation
    assert "raw_market_data_uploaded=false" in annotation
    assert str(tmp_path) not in annotation
    assert str(Path.home()) not in annotation
    assert "\n" not in annotation
    assert "\r" not in annotation
    assert len(annotation.encode("utf-8")) < 1024


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_sha", "../private"),
        ("full_rc_sha", "ABC" * 13 + "A"),
        ("stage", "/Users/private/helper.log"),
        ("exit_code", 0),
        ("exit_code", 256),
    ],
)
def test_publish_failure_evidence_rejects_malformed_inputs(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "output_dir": tmp_path / "publish",
        "candidate_sha": HEAD,
        "full_rc_sha": BASE,
        "stage": "canonical_smoke",
        "exit_code": 1,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        publish_failure_evidence(**kwargs)  # type: ignore[arg-type]


def test_publish_failure_evidence_rejects_non_numeric_run_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        publish_failure_evidence(
            output_dir=tmp_path / "publish",
            candidate_sha=HEAD,
            full_rc_sha=BASE,
            stage="dataset_inventory",
            exit_code=1,
            run_id="3193/private",
        )


def test_publish_failure_evidence_refuses_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / "publish"
    output.mkdir()
    (output / "stale-private.log").write_text("private", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        publish_failure_evidence(
            output_dir=output,
            candidate_sha=HEAD,
            full_rc_sha=BASE,
            stage="canonical_prepare",
            exit_code=1,
        )


def test_publish_failure_evidence_refuses_symlink_output(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "publish"
    output.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        publish_failure_evidence(
            output_dir=output,
            candidate_sha=HEAD,
            full_rc_sha=BASE,
            stage="evidence_publish",
            exit_code=1,
        )
