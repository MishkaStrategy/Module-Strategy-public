from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any


class CoveragePolicyError(ValueError):
    """Raised when coverage evidence is malformed or below policy."""


def _counts(
    covered: object,
    total: object,
    name: str,
    *,
    allow_empty: bool = False,
) -> tuple[int, int]:
    if isinstance(covered, bool) or not isinstance(covered, int) or covered < 0:
        raise CoveragePolicyError(f"{name}.covered must be a non-negative integer")
    minimum_total = 0 if allow_empty else 1
    if isinstance(total, bool) or not isinstance(total, int) or total < minimum_total:
        qualifier = "non-negative" if allow_empty else "positive"
        raise CoveragePolicyError(f"{name}.total must be a {qualifier} integer")
    if covered > total:
        raise CoveragePolicyError(f"{name}.covered cannot exceed total")
    return covered, total


def evaluate(
    payload: dict[str, Any],
    *,
    minimum_statement: Decimal | None = None,
    minimum_branch: Decimal | None = None,
    minimum_combined: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    if (minimum_statement is None) != (minimum_branch is None):
        raise CoveragePolicyError("statement and branch thresholds must be supplied together")
    if minimum_statement is None and minimum_combined is None:
        raise CoveragePolicyError("at least one coverage threshold policy is required")
    for name, threshold in (
        ("statement", minimum_statement),
        ("branch", minimum_branch),
        ("combined", minimum_combined),
    ):
        if threshold is not None and (
            not threshold.is_finite() or not Decimal(0) <= threshold <= Decimal(100)
        ):
            raise CoveragePolicyError(f"minimum {name} coverage must be finite and in [0, 100]")
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise CoveragePolicyError("coverage JSON totals are missing")
    statement_covered, statement_total = _counts(
        totals.get("covered_lines"),
        totals.get("num_statements"),
        "statement",
    )
    branch_covered, branch_total = _counts(
        totals.get("covered_branches"),
        totals.get("num_branches"),
        "branch",
        allow_empty=minimum_branch is None,
    )
    statement = Decimal(statement_covered) * Decimal(100) / Decimal(statement_total)
    if branch_total:
        branch = Decimal(branch_covered) * Decimal(100) / Decimal(branch_total)
    else:
        # With no branch obligations, there are no uncovered branch arcs.
        branch = Decimal(100)
    combined_total = statement_total + branch_total
    if combined_total < 1:
        raise CoveragePolicyError("combined coverage total must be positive")
    combined = Decimal(statement_covered + branch_covered) * Decimal(100) / Decimal(combined_total)
    failures: list[str] = []
    if minimum_statement is not None and statement < minimum_statement:
        failures.append(f"statement coverage {statement:.2f}% is below {minimum_statement:.2f}%")
    if minimum_branch is not None and branch < minimum_branch:
        failures.append(f"branch coverage {branch:.2f}% is below {minimum_branch:.2f}%")
    threshold_ratio = (
        None if minimum_combined is None else Fraction(minimum_combined).as_integer_ratio()
    )
    if threshold_ratio is not None and (
        (statement_covered + branch_covered) * 100 * threshold_ratio[1]
        < combined_total * threshold_ratio[0]
    ):
        failures.append(
            f"combined coverage {combined:.2f}% "
            f"({statement_covered + branch_covered}/{combined_total} obligations) "
            f"is below {minimum_combined:.2f}%"
        )
    if failures:
        raise CoveragePolicyError("; ".join(failures))
    return statement, branch


def _threshold(value: str) -> Decimal:
    try:
        threshold = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("coverage threshold must be numeric") from exc
    if not threshold.is_finite() or not Decimal(0) <= threshold <= Decimal(100):
        raise argparse.ArgumentTypeError("coverage threshold must be in [0, 100]")
    return threshold


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--minimum-statement", type=_threshold)
    parser.add_argument("--minimum-branch", type=_threshold)
    parser.add_argument("--minimum-combined", type=_threshold)
    arguments = parser.parse_args()
    if (arguments.minimum_statement is None) != (arguments.minimum_branch is None):
        parser.error("--minimum-statement and --minimum-branch must be supplied together")
    if arguments.minimum_statement is None and arguments.minimum_combined is None:
        parser.error("supply both statement/branch thresholds or --minimum-combined")
    try:
        payload = json.loads(arguments.evidence.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CoveragePolicyError("coverage JSON root must be an object")
        statement, branch = evaluate(
            payload,
            minimum_statement=arguments.minimum_statement,
            minimum_branch=arguments.minimum_branch,
            minimum_combined=arguments.minimum_combined,
        )
    except (OSError, json.JSONDecodeError, CoveragePolicyError) as exc:
        parser.error(str(exc))
    combined = None
    if arguments.minimum_combined is not None:
        totals = payload["totals"]
        combined_covered = totals["covered_lines"] + totals["covered_branches"]
        combined_obligations = totals["num_statements"] + totals["num_branches"]
        combined = Decimal(combined_covered) * Decimal(100) / Decimal(combined_obligations)
        combined_detail = f" combined={combined:.2f}% ({combined_covered}/{combined_obligations})"
    else:
        combined_detail = ""
    print(
        f"coverage policy passed: statement={statement:.2f}% branch={branch:.2f}%" + combined_detail
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
