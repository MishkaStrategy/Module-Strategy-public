from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from decimal import (
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_UP,
    Decimal,
    localcontext,
)
from fractions import Fraction
from pathlib import Path
from typing import Any

from modular_strategy_trade_plan_router.canonical import decimal_text
from modular_strategy_trade_plan_router.decimal_math import (
    exact_abs_difference,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
)

CONTEXTS = (
    (6, ROUND_DOWN),
    (10, ROUND_HALF_EVEN),
    (28, ROUND_UP),
    (80, ROUND_FLOOR),
)


def _decimal(rng: random.Random) -> Decimal:
    digit_count = rng.randint(1, 30)
    coefficient = str(rng.randint(10 ** (digit_count - 1), (10**digit_count) - 1))
    sign = rng.getrandbits(1)
    exponent = rng.randint(-12, 12)
    return Decimal((sign, tuple(int(digit) for digit in coefficient), exponent))


def _fraction(value: Decimal) -> Fraction:
    numerator, denominator = value.as_integer_ratio()
    return Fraction(numerator, denominator)


def run(*, cases: int, seed: int) -> dict[str, Any]:
    if cases < 1:
        raise ValueError("cases must be positive")
    rng = random.Random(seed)
    digest = hashlib.sha256()
    operations = 0
    started = time.perf_counter_ns()

    for case_id in range(cases):
        left = _decimal(rng)
        right = _decimal(rng)

        expected = {
            "add": _fraction(left) + _fraction(right),
            "subtract": _fraction(left) - _fraction(right),
            "multiply": _fraction(left) * _fraction(right),
            "abs_difference": abs(_fraction(left) - _fraction(right)),
            "sum": _fraction(right),
        }
        baseline: tuple[str, ...] | None = None
        for precision, rounding in CONTEXTS:
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                results = (
                    exact_add(left, right),
                    exact_subtract(left, right),
                    exact_multiply(left, right),
                    exact_abs_difference(left, right),
                    exact_sum((left, right, left.copy_negate())),
                )
            names = tuple(expected)
            for name, result in zip(names, results, strict=True):
                if _fraction(result) != expected[name]:
                    raise AssertionError(f"case {case_id} {name} mismatched Fraction oracle")
            rendered = tuple(decimal_text(result) for result in results)
            if baseline is None:
                baseline = rendered
            elif rendered != baseline:
                raise AssertionError(
                    f"case {case_id} changed under Decimal context {precision}/{rounding}"
                )
            operations += len(results)
        digest.update(
            json.dumps(
                {
                    "case": case_id,
                    "left": decimal_text(left),
                    "right": decimal_text(right),
                    "results": baseline,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    elapsed_ns = time.perf_counter_ns() - started
    return {
        "schema_version": "1",
        "seed": seed,
        "cases": cases,
        "contexts": len(CONTEXTS),
        "operations": operations,
        "elapsed_ms": round(elapsed_ns / 1_000_000, 3),
        "operations_per_second": round(operations / (elapsed_ns / 1_000_000_000), 3),
        "digest": digest.hexdigest(),
        "status": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=28101)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(cases=args.cases, seed=args.seed)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
