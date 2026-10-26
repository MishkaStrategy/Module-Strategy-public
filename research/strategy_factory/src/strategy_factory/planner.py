"""Deterministic architecture and parameter-space planning."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from functools import reduce
from operator import mul
from typing import Any

from .canonical import canonical_sha256
from .contracts import (
    Direction,
    ParameterSpace,
    StrategyArchitecture,
    StrategyFactoryRequest,
    StrategyHypothesis,
)
from .errors import CompatibilityError, ContractError
from .grammar import ArchitectureGenerationResult, StrategyGrammar


class ArchitecturePlanner:
    """Convert falsifiable hypotheses into bounded declarative research spaces."""

    def __init__(self, grammar: StrategyGrammar) -> None:
        self._grammar = grammar

    def generate_architectures(
        self,
        *,
        request: StrategyFactoryRequest,
        hypotheses: tuple[StrategyHypothesis, ...],
        directions: frozenset[Direction],
        final_candidate: bool = False,
        external_features: frozenset[str] = frozenset(),
        runtime_data: frozenset[str] = frozenset(),
    ) -> ArchitectureGenerationResult:
        allowed_signals = frozenset(
            module.module_id for hypothesis in hypotheses for module in hypothesis.module_candidates
        )
        if not allowed_signals:
            raise CompatibilityError("no hypothesis has an eligible registered signal")
        return self._grammar.generate(
            dataset=request.dataset,
            directions=directions,
            forbidden_module_ids=request.forbidden_module_ids,
            maximum_architectures=request.budget.maximum_architectures,
            final_candidate=final_candidate,
            allowed_signal_module_ids=allowed_signals,
            external_features=external_features,
            runtime_data=runtime_data,
        )

    def build_parameter_space(
        self,
        architecture: StrategyArchitecture,
        *,
        maximum_combinations: int,
    ) -> ParameterSpace:
        dimensions: dict[str, tuple[Any, ...]] = {}
        constraints: list[dict[str, Any]] = []
        for module in architecture.runtime_modules:
            for parameter, hint_value in sorted(module.parameter_search_hints.items()):
                schema_value = module.parameter_schema[parameter]
                if not isinstance(schema_value, Mapping) or not isinstance(hint_value, Mapping):
                    raise ContractError(
                        f"parameter schema/search hint for {module.key}:{parameter} must be an object"
                    )
                effective_schema = {**dict(schema_value), **dict(hint_value)}
                full_name = f"{module.runtime_component_id}.{parameter}"
                values = _parameter_values(effective_schema)
                if values is None:
                    raise ContractError(
                        f"searchable parameter {module.key}:{parameter} has no bounded values"
                    )
                dimensions[full_name] = values
                raw_constraints = effective_schema.get("constraints", ())
                if not isinstance(raw_constraints, (tuple, list)):
                    raise ContractError("parameter constraints must be a sequence")
                for item in raw_constraints:
                    if not isinstance(item, Mapping):
                        raise ContractError("parameter constraint must be an object")
                    constraints.append(dict(item))
        if not dimensions:
            dimensions = {"factory.noop_variant": (0,)}
        dimensions = _bound_dimensions(dimensions, maximum_combinations)
        bounded = reduce(mul, (len(values) for values in dimensions.values()), 1)
        if bounded <= 0:
            raise ContractError("maximum_combinations must be positive")
        identity = canonical_sha256(
            {
                "architecture": architecture.fingerprint,
                "dimensions": dimensions,
                "constraints": constraints,
                "maximum_combinations": bounded,
            }
        )
        return ParameterSpace(
            space_id=f"space-{identity[:24]}",
            architecture_id=architecture.architecture_id,
            dimensions=dimensions,
            constraints=tuple(constraints),
            maximum_combinations=bounded,
        )

    def build_seeded_parameter_space(
        self,
        architecture: StrategyArchitecture,
        *,
        seed_parameters: Mapping[str, Any],
        maximum_combinations: int,
        allow_adaptation: bool,
        allowed_parameter_names: frozenset[str] = frozenset(),
        neighborhood_points: int = 3,
    ) -> ParameterSpace:
        """Build a bounded local space that always contains an exact control configuration."""

        if not isinstance(seed_parameters, Mapping) or not seed_parameters:
            raise ContractError("seed_parameters must be a non-empty mapping")
        if not isinstance(allow_adaptation, bool):
            raise ContractError("allow_adaptation must be boolean")
        if (
            isinstance(neighborhood_points, bool)
            or not isinstance(neighborhood_points, int)
            or not 1 <= neighborhood_points <= 21
        ):
            raise ContractError("neighborhood_points must be in [1, 21]")
        base = self.build_parameter_space(
            architecture,
            maximum_combinations=maximum_combinations,
        )
        schema_names = {
            f"{module.runtime_component_id}.{parameter}"
            for module in architecture.runtime_modules
            for parameter in module.parameter_schema
        }
        unknown = sorted(set(seed_parameters) - schema_names)
        if unknown:
            raise CompatibilityError(
                "seed parameters reference unknown runtime fields: " + ", ".join(unknown)
            )
        if allowed_parameter_names:
            unknown_allowed = sorted(allowed_parameter_names - schema_names)
            if unknown_allowed:
                raise CompatibilityError(
                    "allowed parameter names are absent from architecture: "
                    + ", ".join(unknown_allowed)
                )
        dimensions: dict[str, tuple[Any, ...]] = {}
        for name, values in base.dimensions.items():
            if name == "factory.noop_variant":
                continue
            if name not in seed_parameters:
                raise CompatibilityError(
                    f"source package has no value for searchable parameter {name}"
                )
            seed = seed_parameters[name]
            mutable = allow_adaptation and (
                not allowed_parameter_names or name in allowed_parameter_names
            )
            dimensions[name] = (
                _local_seeded_values(values, seed, neighborhood_points) if mutable else (seed,)
            )
        for name, value in sorted(seed_parameters.items()):
            dimensions.setdefault(name, (value,))
        dimensions = _bound_dimensions(dimensions, maximum_combinations)
        bounded = reduce(mul, (len(values) for values in dimensions.values()), 1)
        if bounded <= 0:
            raise ContractError("maximum_combinations must be positive")
        identity = canonical_sha256(
            {
                "architecture": architecture.fingerprint,
                "seed": dict(seed_parameters),
                "dimensions": dimensions,
                "constraints": base.constraints,
                "maximum_combinations": bounded,
            }
        )
        return ParameterSpace(
            space_id=f"space-{identity[:24]}",
            architecture_id=architecture.architecture_id,
            dimensions=dimensions,
            constraints=base.constraints,
            maximum_combinations=bounded,
        )


def _parameter_values(schema: Mapping[str, Any]) -> tuple[Any, ...] | None:
    values = schema.get("values")
    if values is not None:
        if not isinstance(values, (list, tuple)) or not values:
            raise ContractError("parameter values must be a non-empty sequence")
        return tuple(values)
    enum_values = schema.get("enum")
    if enum_values is not None:
        if not isinstance(enum_values, (list, tuple)) or not enum_values:
            raise ContractError("parameter enum must be a non-empty sequence")
        return tuple(enum_values)
    if "default" in schema and "minimum" not in schema and "maximum" not in schema:
        return (schema["default"],)
    if "minimum" not in schema or "maximum" not in schema:
        return None
    minimum = schema["minimum"]
    maximum = schema["maximum"]
    steps = schema.get("factory_steps", 5)
    if not isinstance(steps, int) or isinstance(steps, bool) or not 2 <= steps <= 21:
        raise ContractError("factory_steps must be in [2, 21]")
    if isinstance(minimum, int) and isinstance(maximum, int):
        if maximum < minimum:
            raise ContractError("parameter maximum is below minimum")
        if maximum == minimum:
            return (minimum,)
        span = maximum - minimum
        result = tuple(
            sorted({minimum + round(span * index / (steps - 1)) for index in range(steps)})
        )
        return result
    try:
        lower = Decimal(str(minimum))
        upper = Decimal(str(maximum))
    except Exception as exc:
        raise ContractError("numeric parameter bounds are invalid") from exc
    if not lower.is_finite() or not upper.is_finite() or upper < lower:
        raise ContractError("numeric parameter bounds are invalid")
    if upper == lower:
        return (lower,)
    increment = (upper - lower) / Decimal(steps - 1)
    return tuple(lower + increment * index for index in range(steps))


def _local_seeded_values(
    values: tuple[Any, ...],
    seed: Any,
    maximum_points: int,
) -> tuple[Any, ...]:
    """Return the exact seed plus the closest deterministic registered neighbours."""

    candidates = list(values)
    if isinstance(seed, str):
        candidates = [str(item) for item in candidates]
    elif isinstance(seed, bool):
        candidates = [item for item in candidates if isinstance(item, bool)]
    elif isinstance(seed, int) and not isinstance(seed, bool):
        normalized: list[Any] = []
        for item in candidates:
            try:
                number = Decimal(str(item))
            except (ValueError, TypeError, ArithmeticError):
                normalized.append(item)
                continue
            normalized.append(int(number) if number == number.to_integral_value() else item)
        candidates = normalized
    if not any(item == seed for item in candidates):
        candidates.append(seed)
    try:
        seed_number = Decimal(str(seed))
        if isinstance(seed, bool) or not seed_number.is_finite():
            raise ValueError
        ordered = sorted(
            candidates,
            key=lambda item: (
                abs(Decimal(str(item)) - seed_number),
                canonical_sha256(item),
            ),
        )
    except (ValueError, TypeError, ArithmeticError):
        ordered = sorted(candidates, key=canonical_sha256)
        ordered.remove(seed)
        ordered.insert(0, seed)
    selected = ordered[:maximum_points]
    if not any(item == seed for item in selected):
        selected[-1] = seed
    unique: list[Any] = []
    fingerprints: set[str] = set()
    for item in selected:
        fingerprint = canonical_sha256(item)
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            unique.append(item)
    return tuple(unique)


def _bound_dimensions(
    dimensions: Mapping[str, tuple[Any, ...]],
    maximum_combinations: int,
) -> dict[str, tuple[Any, ...]]:
    """Shrink actual Cartesian dimensions until their product fits the hard budget."""

    if (
        isinstance(maximum_combinations, bool)
        or not isinstance(maximum_combinations, int)
        or maximum_combinations < 1
    ):
        raise ContractError("maximum_combinations must be a positive int")
    bounded = {name: tuple(values) for name, values in dimensions.items()}
    while reduce(mul, (len(values) for values in bounded.values()), 1) > maximum_combinations:
        reducible = [name for name, values in bounded.items() if len(values) > 1]
        if not reducible:
            raise ContractError("parameter dimensions cannot fit the combination budget")
        name = sorted(reducible, key=lambda item: (-len(bounded[item]), item))[0]
        bounded[name] = _evenly_spaced_prefix_preserving(bounded[name], len(bounded[name]) - 1)
    return dict(sorted(bounded.items()))


def _evenly_spaced_prefix_preserving(
    values: tuple[Any, ...],
    target_size: int,
) -> tuple[Any, ...]:
    if target_size >= len(values):
        return values
    if target_size <= 1:
        return (values[0],)
    remaining = values[1:]
    needed = target_size - 1
    if needed >= len(remaining):
        return values
    if needed == 1:
        return (values[0], remaining[len(remaining) // 2])
    indices = tuple(round(index * (len(remaining) - 1) / (needed - 1)) for index in range(needed))
    return (values[0], *(remaining[index] for index in indices))
