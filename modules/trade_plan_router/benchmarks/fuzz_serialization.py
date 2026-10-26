from __future__ import annotations

import argparse
import json
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    RouterInputPackage,
    RouterSerializationError,
    TradePlanSerializer,
)
from modular_strategy_trade_plan_router.canonical import canonical_json

from benchmark_router import NOW, package, portfolio


def _base_payloads(serializer: TradePlanSerializer) -> tuple[str, str]:
    accepted = DecisionRouter().route(package(1))
    rejected = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=portfolio(),
            entries=(),
            protections=(),
            sizings=(),
        )
    )
    return serializer.dumps_result(accepted), serializer.dumps_result(rejected)


def _random_json_value(rng: random.Random) -> Any:
    return rng.choice(
        (
            None,
            True,
            False,
            0,
            -1,
            1,
            "",
            "not-canonical\ntext",
            "NaN",
            [],
            {},
            [1, 2, 3],
            {"unexpected": "field"},
        )
    )


def _all_paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    paths = [prefix]
    if isinstance(value, dict):
        for key, member in value.items():
            paths.extend(_all_paths(member, prefix + (key,)))
    elif isinstance(value, list):
        for index, member in enumerate(value):
            paths.extend(_all_paths(member, prefix + (index,)))
    return paths


def _replace_at_path(value: Any, path: tuple[Any, ...], replacement: Any) -> Any:
    if not path:
        return replacement
    root = json.loads(json.dumps(value, ensure_ascii=False))
    cursor = root
    for member in path[:-1]:
        cursor = cursor[member]
    cursor[path[-1]] = replacement
    return root


def _structured_mutation(base: str, rng: random.Random) -> str:
    value = json.loads(base)
    path = rng.choice(_all_paths(value))
    mutated = _replace_at_path(value, path, _random_json_value(rng))
    return json.dumps(
        mutated,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=bool(rng.getrandbits(1)),
        separators=(",", ":") if rng.getrandbits(1) else None,
    )


def _text_mutation(base: str, rng: random.Random) -> str | bytes:
    mode = rng.randrange(8)
    if mode == 0:
        index = rng.randrange(len(base) + 1)
        return base[:index] + rng.choice(("{", "}", "[", "]", '"', "\\", "\x00")) + base[index:]
    if mode == 1 and base:
        index = rng.randrange(len(base))
        return base[:index] + base[index + 1 :]
    if mode == 2 and base:
        index = rng.randrange(len(base))
        return base[:index] + rng.choice(("x", "0", "é", "\ud800")) + base[index + 1 :]
    if mode == 3:
        return base[: rng.randrange(len(base) + 1)]
    if mode == 4:
        return rng.choice(("garbage", "[]", "null", "NaN", "Infinity")) + base
    if mode == 5:
        return base + rng.choice(("garbage", "[]", "\x00"))
    if mode == 6:
        return base.encode("utf-8") + b"\xff"
    return _structured_mutation(base, rng)


def run(seed: int, cases: int) -> dict[str, Any]:
    if cases < 1:
        raise ValueError("cases must be positive")
    rng = random.Random(seed)
    serializer = TradePlanSerializer()
    bases = _base_payloads(serializer)
    outcomes: Counter[str] = Counter()
    started = time.perf_counter_ns()

    for _case_id in range(cases):
        payload = _text_mutation(rng.choice(bases), rng)
        try:
            loaded = serializer.loads_result_mapping(payload)
        except RouterSerializationError:
            outcomes["typed_rejection"] += 1
            continue
        except Exception as exc:  # pragma: no cover - benchmark failure evidence
            raise AssertionError(
                f"unexpected exception escaped serialization boundary: {type(exc).__name__}: {exc}"
            ) from exc

        canonical = canonical_json(loaded)
        replay = serializer.loads_result_mapping(canonical)
        if canonical_json(replay) != canonical:
            raise AssertionError("accepted mutated payload was not replay-stable")
        outcomes["accepted_valid_mapping"] += 1

    elapsed_ns = time.perf_counter_ns() - started
    return {
        "schema_version": "1",
        "seed": seed,
        "cases": cases,
        "elapsed_ms": round(elapsed_ns / 1_000_000, 3),
        "cases_per_second": round(cases / (elapsed_ns / 1_000_000_000), 3),
        "outcomes": dict(sorted(outcomes.items())),
        "status": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=28023)
    parser.add_argument("--cases", type=int, default=50_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.seed, args.cases)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
