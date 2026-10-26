from __future__ import annotations

import itertools
import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .canonical import canonical_sha256
from .models import SearchSpace, SpecificationError


@dataclass(frozen=True, slots=True)
class GeneratedConfiguration:
    index: int
    parameters: Mapping[str, Any]
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


def _raw_size(space: SearchSpace) -> int:
    return math.prod(len(dimension.values) for dimension in space.dimensions)


def _accepted(space: SearchSpace, values: Mapping[str, Any]) -> bool:
    return all(constraint.accepts(values) for constraint in space.constraints)


def _configuration(index: int, values: Mapping[str, Any]) -> GeneratedConfiguration:
    parameters = dict(values)
    return GeneratedConfiguration(
        index=index,
        parameters=parameters,
        sha256=canonical_sha256(parameters),
    )


class GridSearchPlanner:
    def generate(self, space: SearchSpace) -> tuple[GeneratedConfiguration, ...]:
        total = _raw_size(space)
        if total > space.maximum_combinations:
            raise SpecificationError(
                f"grid search contains {total} combinations; limit is {space.maximum_combinations}"
            )
        names = tuple(item.name for item in space.dimensions)
        values = tuple(item.values for item in space.dimensions)
        generated: list[GeneratedConfiguration] = []
        seen: set[str] = set()
        for combination in itertools.product(*values):
            parameters = dict(zip(names, combination, strict=True))
            if not _accepted(space, parameters):
                continue
            item = _configuration(len(generated), parameters)
            if item.sha256 in seen:
                raise SpecificationError("search space produced a duplicate configuration")
            seen.add(item.sha256)
            generated.append(item)
        if not generated:
            raise SpecificationError("all grid configurations were rejected by constraints")
        return tuple(generated)


class RandomSearchPlanner:
    def generate(
        self,
        space: SearchSpace,
        *,
        sample_count: int,
        seed: int,
    ) -> tuple[GeneratedConfiguration, ...]:
        if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 1:
            raise SpecificationError("sample_count must be a positive integer")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise SpecificationError("random-search seed must be an integer")
        population = GridSearchPlanner().generate(space)
        if sample_count > len(population):
            raise SpecificationError("sample_count exceeds the number of valid configurations")
        rng = random.Random(seed)  # nosec B311 - deterministic research sampling only
        selected_indices = rng.sample(range(len(population)), sample_count)
        return tuple(
            GeneratedConfiguration(
                index=index,
                parameters=population[source_index].parameters,
                sha256=population[source_index].sha256,
            )
            for index, source_index in enumerate(selected_indices)
        )


def configuration_hashes(configurations: Iterable[GeneratedConfiguration]) -> tuple[str, ...]:
    return tuple(item.sha256 for item in configurations)
