from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import canonical_json_bytes, canonical_sha256
from .errors import IntegrityError, ValidationError
from .evidence import StrategyAuditEvidencePackage
from .safe_io import atomic_write_bytes


@dataclass(frozen=True, slots=True)
class RenderedReport:
    report_id: str
    schema_version: str
    media_type: str
    content: bytes
    content_hash: str

    def __post_init__(self) -> None:
        if not self.report_id or not self.media_type or not self.schema_version:
            raise ValidationError("report metadata must be non-empty")
        if canonical_sha256({"bytes_hex": self.content.hex()}) != self.content_hash:
            raise IntegrityError("rendered report hash mismatch")


@dataclass(frozen=True, slots=True)
class ResearchReportInput:
    title: str
    executive_summary: str
    audit_package: StrategyAuditEvidencePackage
    next_step_recommendation: str

    def __post_init__(self) -> None:
        for value in (self.title, self.executive_summary, self.next_step_recommendation):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("report text fields must be non-blank")


def _report(
    package_id: str,
    media_type: str,
    content: bytes,
    *,
    schema_version: str = "research-report.v1",
) -> RenderedReport:
    payload_hash = canonical_sha256({"bytes_hex": content.hex()})
    suffix = {"application/json": "json", "text/markdown": "md", "text/html": "html"}[media_type]
    return RenderedReport(
        report_id=f"report-{package_id}-{suffix}",
        schema_version=schema_version,
        media_type=media_type,
        content=content,
        content_hash=payload_hash,
    )


def render_json_report(data: ResearchReportInput) -> RenderedReport:
    payload = {
        "schema_version": "research-report.v1",
        "title": data.title,
        "executive_summary": data.executive_summary,
        "audit_package": data.audit_package.to_mapping(),
        "next_step_recommendation": data.next_step_recommendation,
    }
    return _report(data.audit_package.package_id, "application/json", canonical_json_bytes(payload))


def render_markdown_report(data: ResearchReportInput) -> RenderedReport:
    package = data.audit_package
    lines = [
        f"# {data.title}",
        "",
        "## Executive summary",
        "",
        data.executive_summary,
        "",
        "## Registry integrity",
        "",
        f"- Registry revision: `{package.registry_revision}`",
        f"- Registry state hash: `{package.registry_state_hash}`",
        f"- Integrity status: **{package.registry_integrity_status}**",
        f"- Reproducibility status: **{package.reproducibility_status}**",
        f"- Audit package hash: `{package.package_hash}`",
        "",
        "## Experiment and dataset lineage",
        "",
        _bullet("Experiments", package.experiment_ids),
        _bullet("Runs", package.run_ids),
        _bullet("Configurations", package.configuration_ids),
        _bullet("Datasets", package.dataset_ids),
        _bullet("Dataset hashes", package.dataset_hashes),
        _bullet("Commits", package.commit_shas),
        "",
        "## Baseline and candidates",
        "",
        _bullet("Baselines", package.baseline_ids),
        _bullet("Candidates", package.candidate_ids),
        "",
        "## Compatibility and metric comparisons",
        "",
        _mapping_table(package.comparison_tables),
        "",
        "## Registered metric snapshots",
        "",
        _mapping_table(package.metrics),
        "",
        "## Slices",
        "",
    ]
    if package.slices:
        for dimension in sorted(package.slices):
            lines.extend((f"### {dimension}", "", _mapping_table(package.slices[dimension]), ""))
    else:
        lines.extend(("No slice artifacts were supplied.", ""))
    lines.extend(
        (
            "## Pareto frontier",
            "",
            _pretty_mapping(package.pareto_result),
            "",
            "## Stability and statistical-strength findings",
            "",
            _finding_table(package.findings),
            "",
            "## Audit evidence and completeness",
            "",
            f"Completeness: **{package.completeness.status.value}**",
            "",
            _bullet("Satisfied", package.completeness.satisfied),
            _bullet("Missing", package.completeness.missing),
            _bullet("Failed", package.completeness.failed),
            _bullet("Blocking reasons", package.completeness.blocking_reasons),
            "",
            "## Contamination and known limitations",
            "",
            _bullet("Contamination flags", package.contamination_flags),
            _bullet("Known limitations", package.known_limitations),
            "",
            "## Next-step recommendation",
            "",
            data.next_step_recommendation,
            "",
            "> Pareto ranking is analytical evidence only and is not an automatic production decision.",
            "",
        )
    )
    content = "\n".join(lines).encode("utf-8")
    return _report(package.package_id, "text/markdown", content)


def render_html_report(data: ResearchReportInput) -> RenderedReport:
    markdown = render_markdown_report(data).content.decode("utf-8")
    body = "\n".join(
        f"<p>{html.escape(line)}</p>" if line else "" for line in markdown.splitlines()
    )
    content = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        "<title>"
        + html.escape(data.title)
        + "</title></head><body>\n"
        + body
        + "\n</body></html>\n"
    ).encode("utf-8")
    return _report(data.audit_package.package_id, "text/html", content)


def render_csv(rows: Iterable[Mapping[str, Any]]) -> bytes:
    materialized = tuple(rows)
    fieldnames = sorted({str(key) for row in materialized for key in row})
    if not fieldnames:
        return b""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=fieldnames, lineterminator="\n", extrasaction="raise"
    )
    writer.writeheader()
    for row in materialized:
        writer.writerow({key: _safe_csv_cell(row.get(key, "")) for key in fieldnames})
    return output.getvalue().encode("utf-8")


def write_report_atomic(report: RenderedReport, path: str | Path) -> Path:
    return atomic_write_bytes(path, report.content)


def _safe_csv_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def _bullet(label: str, values: tuple[str, ...]) -> str:
    return f"- {label}: " + (", ".join(f"`{item}`" for item in values) if values else "none")


def _pretty_mapping(value: Mapping[str, Any]) -> str:
    if not value:
        return "No data supplied."
    return "\n".join(f"- `{key}`: `{value[key]}`" for key in sorted(value))


def _mapping_table(rows: tuple[Mapping[str, Any], ...]) -> str:
    if not rows:
        return "No data supplied."
    columns = sorted({str(key) for row in rows for key in row})
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        body.append(
            "| " + " | ".join(_markdown_cell(row.get(column, "")) for column in columns) + " |"
        )
    return "\n".join((header, separator, *body))


def _finding_table(findings: tuple[Any, ...]) -> str:
    rows = tuple(
        {
            "code": item.code.value,
            "severity": item.severity.value,
            "blocking": item.blocking,
            "actual": item.actual_value,
            "threshold": item.threshold,
            "recommendation": item.recommendation,
        }
        for item in findings
    )
    return _mapping_table(rows)


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")
