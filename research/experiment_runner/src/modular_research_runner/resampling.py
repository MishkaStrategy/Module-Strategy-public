from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .canonical import canonical_sha256
from .identity import derive_seed
from .models import SpecificationError

_DEFAULT_MAXIMUM_TOTAL_INDICES = 10_000_000


def _positive_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SpecificationError(f"{name} must be a positive integer")
    return value


def _nonnegative_seed(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpecificationError("root_seed must be a non-negative integer")
    return value


def _enforce_plan_size(
    *,
    population_size: int,
    plan_count: int,
    maximum_total_indices: int,
) -> None:
    if population_size * plan_count > maximum_total_indices:
        raise SpecificationError("resampling plan exceeds maximum_total_indices before allocation")


class BootstrapMethod(StrEnum):
    IID = "iid"
    MOVING_BLOCK = "moving_block"


@dataclass(frozen=True, slots=True)
class ResampleIndexPlan:
    sample_id: str
    seed: int
    indices: tuple[int, ...]
    method: BootstrapMethod
    block_size: int | None = None

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise SpecificationError("sample_id must be non-empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise SpecificationError("resample seed must be a non-negative integer")
        if not isinstance(self.indices, tuple) or not self.indices:
            raise SpecificationError("resample plan must contain indices")
        if any(isinstance(index, bool) or not isinstance(index, int) for index in self.indices):
            raise SpecificationError("resample indices must be an integer tuple")
        if any(index < 0 for index in self.indices):
            raise SpecificationError("resample indices must be non-negative")
        if not isinstance(self.method, BootstrapMethod):
            raise SpecificationError("resample method must be a BootstrapMethod")
        if self.method is BootstrapMethod.MOVING_BLOCK:
            if self.block_size is None or self.block_size < 1:
                raise SpecificationError("moving-block bootstrap requires a positive block size")
        elif self.block_size is not None:
            raise SpecificationError("IID bootstrap cannot define block_size")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


class BootstrapPlanner:
    def __init__(
        self,
        maximum_total_indices: int = _DEFAULT_MAXIMUM_TOTAL_INDICES,
    ) -> None:
        self.maximum_total_indices = _positive_integer(
            "maximum_total_indices",
            maximum_total_indices,
        )

    def generate(
        self,
        *,
        population_size: int,
        sample_count: int,
        root_seed: int,
        method: BootstrapMethod = BootstrapMethod.IID,
        block_size: int | None = None,
    ) -> tuple[ResampleIndexPlan, ...]:
        population_size = _positive_integer("population_size", population_size)
        sample_count = _positive_integer("sample_count", sample_count)
        root_seed = _nonnegative_seed(root_seed)
        _enforce_plan_size(
            population_size=population_size,
            plan_count=sample_count,
            maximum_total_indices=self.maximum_total_indices,
        )
        try:
            resolved_method = BootstrapMethod(method)
        except ValueError as exc:
            raise SpecificationError("unsupported bootstrap method") from exc
        if resolved_method is BootstrapMethod.MOVING_BLOCK:
            if block_size is None or block_size < 1 or block_size > population_size:
                raise SpecificationError("block_size must be between one and population_size")
        elif block_size is not None:
            raise SpecificationError("IID bootstrap cannot define block_size")
        plans: list[ResampleIndexPlan] = []
        for sample_index in range(sample_count):
            seed = derive_seed(root_seed, "bootstrap", resolved_method.value, str(sample_index))
            rng = random.Random(seed)  # nosec B311 - deterministic research sampling only
            if resolved_method is BootstrapMethod.IID:
                indices = tuple(rng.randrange(population_size) for _ in range(population_size))
            else:
                if block_size is None:
                    raise SpecificationError("moving-block bootstrap requires block_size")
                values: list[int] = []
                while len(values) < population_size:
                    start = rng.randrange(population_size)
                    values.extend(
                        (start + offset) % population_size for offset in range(block_size)
                    )
                indices = tuple(values[:population_size])
            digest = canonical_sha256(
                {
                    "population_size": population_size,
                    "sample_index": sample_index,
                    "seed": seed,
                    "method": resolved_method,
                    "block_size": block_size,
                    "indices": indices,
                }
            )
            plans.append(
                ResampleIndexPlan(
                    sample_id=f"bootstrap-{sample_index:05d}-{digest[:12]}",
                    seed=seed,
                    indices=indices,
                    method=resolved_method,
                    block_size=block_size,
                )
            )
        return tuple(plans)


@dataclass(frozen=True, slots=True)
class SignalSkipPlan:
    scenario_id: str
    seed: int
    keep_mask: tuple[bool, ...]
    skip_probability: float

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise SpecificationError("signal skip scenario_id must be non-empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise SpecificationError("signal skip seed must be a non-negative integer")
        if (
            not isinstance(self.skip_probability, int | float)
            or isinstance(self.skip_probability, bool)
            or not 0 <= self.skip_probability < 1
        ):
            raise SpecificationError("skip_probability must be numeric and in [0, 1)")
        if (
            not isinstance(self.keep_mask, tuple)
            or not self.keep_mask
            or any(not isinstance(value, bool) for value in self.keep_mask)
        ):
            raise SpecificationError("signal skip keep_mask must be a non-empty boolean tuple")
        if not any(self.keep_mask):
            raise SpecificationError("signal skip plan must retain at least one signal")


class MonteCarloMethod(StrEnum):
    PERMUTATION = "permutation"
    BLOCK_PERMUTATION = "block_permutation"


@dataclass(frozen=True, slots=True)
class MonteCarloPathPlan:
    scenario_id: str
    seed: int
    indices: tuple[int, ...]
    method: MonteCarloMethod
    block_size: int | None = None

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise SpecificationError("Monte Carlo scenario_id must be non-empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise SpecificationError("Monte Carlo seed must be a non-negative integer")
        if not isinstance(self.indices, tuple) or not self.indices:
            raise SpecificationError("Monte Carlo path must contain indices")
        if any(isinstance(index, bool) or not isinstance(index, int) for index in self.indices):
            raise SpecificationError("Monte Carlo path indices must be an integer tuple")
        if not isinstance(self.method, MonteCarloMethod):
            raise SpecificationError("Monte Carlo method must be a MonteCarloMethod")
        if sorted(self.indices) != list(range(len(self.indices))):
            raise SpecificationError(
                "Monte Carlo path indices must be a complete zero-based permutation"
            )
        if self.method is MonteCarloMethod.BLOCK_PERMUTATION:
            if self.block_size is None or not 1 <= self.block_size <= len(self.indices):
                raise SpecificationError(
                    "block permutation requires block_size within the path length"
                )
        elif self.block_size is not None:
            raise SpecificationError("plain permutation cannot define block_size")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


class MonteCarloPlanner:
    """Builds deterministic sequence-path scenarios without calculating metrics."""

    def __init__(
        self,
        maximum_total_indices: int = _DEFAULT_MAXIMUM_TOTAL_INDICES,
    ) -> None:
        self.maximum_total_indices = _positive_integer(
            "maximum_total_indices",
            maximum_total_indices,
        )

    def generate(
        self,
        *,
        population_size: int,
        scenario_count: int,
        root_seed: int,
        method: MonteCarloMethod = MonteCarloMethod.PERMUTATION,
        block_size: int | None = None,
    ) -> tuple[MonteCarloPathPlan, ...]:
        population_size = _positive_integer("population_size", population_size)
        scenario_count = _positive_integer("scenario_count", scenario_count)
        root_seed = _nonnegative_seed(root_seed)
        _enforce_plan_size(
            population_size=population_size,
            plan_count=scenario_count,
            maximum_total_indices=self.maximum_total_indices,
        )
        try:
            resolved_method = MonteCarloMethod(method)
        except ValueError as exc:
            raise SpecificationError("unsupported Monte Carlo method") from exc
        if resolved_method is MonteCarloMethod.BLOCK_PERMUTATION:
            if block_size is None or not 1 <= block_size <= population_size:
                raise SpecificationError("block_size must be between one and population_size")
        elif block_size is not None:
            raise SpecificationError("plain permutation cannot define block_size")

        plans: list[MonteCarloPathPlan] = []
        for scenario_index in range(scenario_count):
            seed = derive_seed(
                root_seed,
                "monte-carlo",
                resolved_method.value,
                str(block_size),
                str(scenario_index),
            )
            rng = random.Random(seed)  # nosec B311 - deterministic research sampling only
            if resolved_method is MonteCarloMethod.PERMUTATION:
                path = list(range(population_size))
                rng.shuffle(path)
            else:
                if block_size is None:
                    raise SpecificationError("block Monte Carlo requires block_size")
                blocks = [
                    list(range(start, min(start + block_size, population_size)))
                    for start in range(0, population_size, block_size)
                ]
                rng.shuffle(blocks)
                path = [index for block in blocks for index in block]
            indices = tuple(path)
            digest = canonical_sha256(
                {
                    "population_size": population_size,
                    "scenario_index": scenario_index,
                    "seed": seed,
                    "method": resolved_method,
                    "block_size": block_size,
                    "indices": indices,
                }
            )
            plans.append(
                MonteCarloPathPlan(
                    scenario_id=f"monte-carlo-{scenario_index:05d}-{digest[:12]}",
                    seed=seed,
                    indices=indices,
                    method=resolved_method,
                    block_size=block_size,
                )
            )
        return tuple(plans)


class SignalSkipPlanner:
    def __init__(
        self,
        maximum_total_indices: int = _DEFAULT_MAXIMUM_TOTAL_INDICES,
    ) -> None:
        self.maximum_total_indices = _positive_integer(
            "maximum_total_indices",
            maximum_total_indices,
        )

    def generate(
        self,
        *,
        signal_count: int,
        scenario_count: int,
        skip_probability: float,
        root_seed: int,
    ) -> tuple[SignalSkipPlan, ...]:
        signal_count = _positive_integer("signal_count", signal_count)
        scenario_count = _positive_integer("scenario_count", scenario_count)
        root_seed = _nonnegative_seed(root_seed)
        _enforce_plan_size(
            population_size=signal_count,
            plan_count=scenario_count,
            maximum_total_indices=self.maximum_total_indices,
        )
        if not isinstance(skip_probability, int | float) or isinstance(skip_probability, bool):
            raise SpecificationError("skip_probability must be numeric")
        probability = float(skip_probability)
        if not 0 <= probability < 1:
            raise SpecificationError("skip_probability must be in [0, 1)")
        plans: list[SignalSkipPlan] = []
        for index in range(scenario_count):
            seed = derive_seed(root_seed, "signal-skip", str(index))
            rng = random.Random(seed)  # nosec B311 - deterministic research sampling only
            mask = [rng.random() >= probability for _ in range(signal_count)]
            if not any(mask):
                mask[rng.randrange(signal_count)] = True
            digest = canonical_sha256({"seed": seed, "mask": mask, "skip_probability": probability})
            plans.append(
                SignalSkipPlan(
                    scenario_id=f"signal-skip-{index:05d}-{digest[:12]}",
                    seed=seed,
                    keep_mask=tuple(mask),
                    skip_probability=probability,
                )
            )
        return tuple(plans)


class SequenceResampler[T]:
    """Applies a precomputed index plan without calculating trading metrics."""

    @staticmethod
    def apply(values: Sequence[T], plan: ResampleIndexPlan | MonteCarloPathPlan) -> tuple[T, ...]:
        if not values:
            raise SpecificationError("cannot resample an empty sequence")
        if any(index >= len(values) for index in plan.indices):
            raise SpecificationError("resample plan references an out-of-range index")
        return tuple(values[index] for index in plan.indices)
