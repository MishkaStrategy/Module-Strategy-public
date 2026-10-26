from __future__ import annotations

import importlib
import inspect
import math
import os
import re
import stat
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from types import MappingProxyType

from .data import timeframe_to_seconds
from .validation import require_nonblank


_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
_API_VERSION_RE = re.compile(r"^\d+\.\d+$")
_PARAMETER_TYPES = frozenset({"number", "integer", "string", "boolean", "array", "object"})
_PARAMETER_RULE_KEYS = frozenset({"type", "required", "minimum", "maximum", "enum", "default"})


class ModuleCompatibilityError(ValueError):
    pass


class ModuleDependencyError(ValueError):
    pass


class ModuleConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    name: str
    version: str = "0.1.0"
    api_version: str = "1.0"
    required_timeframes: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    optional_features: tuple[str, ...] = ()
    produced_features: tuple[str, ...] = ()
    warmup_bars: Mapping[str, int] = field(default_factory=dict)
    parameters_schema: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    skip_until_ready: bool = True
    description: str = ""
    state_schema_version: str = "1"

    def __post_init__(self) -> None:
        name = _canonical_string(self.name, "manifest name")
        version = _canonical_string(self.version, "manifest version")
        api_version = _canonical_string(self.api_version, "manifest api_version")
        state_schema = _canonical_string(self.state_schema_version, "manifest state_schema_version")
        if not isinstance(self.description, str):
            raise TypeError("manifest description must be str")
        if not isinstance(self.skip_until_ready, bool):
            raise TypeError("manifest skip_until_ready must be bool")
        if not _VERSION_RE.fullmatch(version):
            raise ValueError("manifest version is invalid")
        if not _API_VERSION_RE.fullmatch(api_version):
            raise ValueError("manifest api_version must be MAJOR.MINOR")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "api_version", api_version)
        object.__setattr__(self, "state_schema_version", state_schema)

        for field_name in (
            "required_timeframes",
            "required_features",
            "optional_features",
            "produced_features",
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_string_tuple(getattr(self, field_name), field_name),
            )

        collections = {
            "required_timeframes": self.required_timeframes,
            "required_features": self.required_features,
            "optional_features": self.optional_features,
            "produced_features": self.produced_features,
        }
        for label, values in collections.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{label} contains duplicates")
        for timeframe in self.required_timeframes:
            if timeframe_to_seconds(timeframe) is None:
                raise ValueError(f"invalid required timeframe: {timeframe!r}")
        all_features = (
            *self.required_features,
            *self.optional_features,
            *self.produced_features,
        )
        for value in all_features:
            _validate_feature_name(value)
        if set(self.required_features) & set(self.optional_features):
            raise ValueError("feature cannot be both required and optional")
        if set(self.required_features) & set(self.produced_features):
            raise ValueError("feature cannot be both required and produced")

        if not isinstance(self.warmup_bars, Mapping):
            raise TypeError("warmup_bars must be a mapping")
        frozen_warmup: dict[str, int] = {}
        for raw_timeframe, bars in self.warmup_bars.items():
            timeframe = _canonical_string(raw_timeframe, "warmup timeframe")
            if timeframe_to_seconds(timeframe) is None:
                raise ValueError(f"invalid warmup timeframe: {timeframe!r}")
            if not isinstance(bars, int) or isinstance(bars, bool):
                raise TypeError("warmup_bars must contain integer counts")
            if bars < 0:
                raise ValueError("warmup_bars counts cannot be negative")
            frozen_warmup[timeframe] = bars

        _validate_parameter_schema(self.parameters_schema)
        frozen_schema = _freeze_mapping(self.parameters_schema, "parameters_schema")
        object.__setattr__(self, "warmup_bars", MappingProxyType(frozen_warmup))
        object.__setattr__(self, "parameters_schema", frozen_schema)


@dataclass(frozen=True, slots=True)
class DependencyPlan:
    modules: tuple[Any, ...]
    manifests: tuple[ModuleManifest, ...]
    produced_by: Mapping[str, str]
    required_timeframes: tuple[str, ...]

    def __post_init__(self) -> None:
        modules = _plain_tuple(self.modules, "modules")
        manifests = _typed_tuple(self.manifests, ModuleManifest, "manifests")
        if len(modules) != len(manifests):
            raise ValueError("modules and manifests must have equal length")
        for module, manifest in zip(modules, manifests):
            module_name = getattr(module, "name", None)
            if module_name is not None:
                module_name = _canonical_string(module_name, "module name")
                if module_name != manifest.name:
                    raise ValueError(
                        f"module.name {module_name!r} does not match "
                        f"manifest.name {manifest.name!r}"
                    )
        if not isinstance(self.produced_by, Mapping):
            raise TypeError("produced_by must be a mapping")
        produced_by: dict[str, str] = {}
        for raw_feature, raw_module in self.produced_by.items():
            feature = _canonical_string(raw_feature, "produced_by feature")
            module = _canonical_string(raw_module, "produced_by module")
            _validate_feature_name(feature)
            produced_by[feature] = module
        expected_producers: dict[str, str] = {}
        for manifest in manifests:
            for feature in manifest.produced_features:
                if feature in expected_producers:
                    raise ValueError(f"feature {feature!r} has multiple manifest producers")
                expected_producers[feature] = manifest.name
        if produced_by != expected_producers:
            raise ValueError("produced_by is inconsistent with manifests")
        required = _canonical_string_tuple(self.required_timeframes, "required_timeframes")
        if len(required) != len(set(required)):
            raise ValueError("required_timeframes contains duplicates")
        for timeframe in required:
            if timeframe_to_seconds(timeframe) is None:
                raise ValueError(f"invalid required timeframe: {timeframe!r}")
        object.__setattr__(self, "modules", modules)
        object.__setattr__(self, "manifests", manifests)
        object.__setattr__(self, "produced_by", MappingProxyType(produced_by))
        object.__setattr__(self, "required_timeframes", required)


class ModuleRegistry:
    """Discovers, loads, validates and orders modules without kernel edits."""

    def __init__(
        self,
        supported_api_major: int = 1,
        *,
        max_config_bytes: int | None = 1024 * 1024,
        max_yaml_nodes: int = 100_000,
    ) -> None:
        if not isinstance(supported_api_major, int) or isinstance(supported_api_major, bool):
            raise TypeError("supported_api_major must be int")
        if supported_api_major < 1:
            raise ValueError("supported_api_major must be positive")
        if max_config_bytes is not None:
            if not isinstance(max_config_bytes, int) or isinstance(max_config_bytes, bool):
                raise TypeError("max_config_bytes must be int or None")
            if max_config_bytes < 1:
                raise ValueError("max_config_bytes must be positive")
        if not isinstance(max_yaml_nodes, int) or isinstance(max_yaml_nodes, bool):
            raise TypeError("max_yaml_nodes must be int")
        if max_yaml_nodes < 1:
            raise ValueError("max_yaml_nodes must be positive")
        self.supported_api_major = supported_api_major
        self.max_config_bytes = max_config_bytes
        self.max_yaml_nodes = max_yaml_nodes
        self._factories: dict[str, Any] = {}

    def register(self, name: str, factory: Any) -> None:
        require_nonblank(name, "module registration name")
        if name != name.strip():
            raise ValueError("module registration name cannot contain surrounding whitespace")
        if name in self._factories:
            raise ValueError(f"module already registered: {name}")
        self._factories[name] = factory

    def discover_entry_points(self, group: str = "modular_strategy.modules") -> tuple[str, ...]:
        group = _canonical_string(group, "entry point group")
        discovered: list[str] = []
        selected = entry_points().select(group=group)
        for item in selected:
            self.register(item.name, item.load())
            discovered.append(item.name)
        return tuple(discovered)

    def load_import_path(
        self, import_path: str, parameters: Mapping[str, Any] | None = None
    ) -> Any:
        import_path = _canonical_string(import_path, "module import path")
        module_path, separator, attribute = import_path.partition(":")
        if not separator:
            module_path, _, attribute = import_path.rpartition(".")
        if not module_path or not attribute:
            raise ModuleConfigurationError(
                "module path must be 'package.module:Class' or 'package.module.Class'"
            )
        target = getattr(importlib.import_module(module_path), attribute)
        return self._instantiate(target, parameters or {})

    def load_named(self, name: str, parameters: Mapping[str, Any] | None = None) -> Any:
        name = _canonical_string(name, "registered module name")
        if name not in self._factories:
            raise ModuleConfigurationError(f"unknown registered module: {name}")
        return self._instantiate(self._factories[name], parameters or {})

    def load_config(self, config: str | Path | Mapping[str, Any]) -> tuple[Any, ...]:
        if isinstance(config, (str, Path)):
            text = _read_bounded_regular_text(Path(config), self.max_config_bytes)
            payload = _safe_load_yaml(text, self.max_yaml_nodes)
        else:
            if not isinstance(config, Mapping):
                raise TypeError("module config must be a path or mapping")
            payload = dict(config)
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise ModuleConfigurationError("module config root must be a mapping")
        root = _strict_string_mapping(payload, "module config root")
        unknown_root = sorted(set(root) - {"modules"})
        if unknown_root:
            raise ModuleConfigurationError(f"unknown module config keys: {', '.join(unknown_root)}")
        rows = root.get("modules", ())
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ModuleConfigurationError("config.modules must be a sequence")
        loaded: list[Any] = []
        allowed_row_keys = {"class", "plugin", "enabled", "parameters"}
        for index, raw_row in enumerate(rows):
            row: Mapping[str, Any]
            if isinstance(raw_row, str):
                row = {"class": raw_row}
            elif isinstance(raw_row, Mapping):
                row = _strict_string_mapping(raw_row, f"modules[{index}]")
            else:
                raise ModuleConfigurationError("every module config must be a mapping")
            unknown = sorted(set(row) - allowed_row_keys)
            if unknown:
                raise ModuleConfigurationError(
                    f"unknown keys in modules[{index}]: {', '.join(unknown)}"
                )
            enabled = row.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ModuleConfigurationError(f"modules[{index}].enabled must be boolean")
            if not enabled:
                continue
            has_class = "class" in row
            has_plugin = "plugin" in row
            if has_class == has_plugin:
                raise ModuleConfigurationError(
                    f"modules[{index}] requires exactly one of 'class' or 'plugin'"
                )
            raw_parameters = row.get("parameters", {})
            if not isinstance(raw_parameters, Mapping):
                raise ModuleConfigurationError(f"modules[{index}].parameters must be a mapping")
            parameters = _strict_string_mapping(raw_parameters, f"modules[{index}].parameters")
            if has_class:
                class_path = row["class"]
                if not isinstance(class_path, str):
                    raise ModuleConfigurationError(f"modules[{index}].class must be str")
                loaded.append(
                    self.load_import_path(
                        _canonical_string(class_path, f"modules[{index}].class"),
                        parameters,
                    )
                )
            else:
                plugin_name = row["plugin"]
                if not isinstance(plugin_name, str):
                    raise ModuleConfigurationError(f"modules[{index}].plugin must be str")
                loaded.append(
                    self.load_named(
                        _canonical_string(plugin_name, f"modules[{index}].plugin"),
                        parameters,
                    )
                )
        return tuple(loaded)

    def validate_and_plan(self, modules: Iterable[Any], *, base_timeframe: str) -> DependencyPlan:
        base_timeframe = _canonical_string(base_timeframe, "base_timeframe")
        if timeframe_to_seconds(base_timeframe) is None:
            raise ValueError(f"invalid base timeframe: {base_timeframe!r}")
        module_list = tuple(modules)
        manifests = tuple(manifest_for(module) for module in module_list)
        names = [manifest.name for manifest in manifests]
        if len(names) != len(set(names)):
            raise ModuleCompatibilityError("module manifest names must be unique")
        for module, manifest in zip(module_list, manifests):
            if getattr(module, "name", None) != manifest.name:
                raise ModuleCompatibilityError(
                    f"module.name {getattr(module, 'name', None)!r} does not match manifest.name {manifest.name!r}"
                )
            self._validate_api(manifest)
            validate_parameters(manifest, parameters_for(module))

        produced_by: dict[str, str] = {}
        for manifest in manifests:
            for feature in manifest.produced_features:
                if feature in produced_by:
                    raise ModuleDependencyError(
                        f"feature {feature!r} has multiple producers: "
                        f"{produced_by[feature]!r}, {manifest.name!r}"
                    )
                produced_by[feature] = manifest.name

        missing: list[str] = []
        for manifest in manifests:
            for feature in manifest.required_features:
                producer = produced_by.get(feature)
                if producer is None:
                    missing.append(f"{manifest.name} requires {feature}")
                elif producer == manifest.name:
                    raise ModuleDependencyError(
                        f"module {manifest.name!r} cannot require its own produced feature {feature!r}"
                    )
        if missing:
            raise ModuleDependencyError("missing module dependencies: " + "; ".join(missing))

        by_name = {manifest.name: module for manifest, module in zip(manifests, module_list)}
        manifest_by_name = {manifest.name: manifest for manifest in manifests}
        dependencies: dict[str, set[str]] = {name: set() for name in names}
        for manifest in manifests:
            for feature in manifest.required_features:
                dependencies[manifest.name].add(produced_by[feature])

        original_index = {name: index for index, name in enumerate(names)}
        ordered_names: list[str] = []
        remaining = {name: set(deps) for name, deps in dependencies.items()}
        while remaining:
            ready = sorted(
                (name for name, deps in remaining.items() if not deps),
                key=original_index.__getitem__,
            )
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise ModuleDependencyError(f"cyclic module dependencies: {cycle}")
            for name in ready:
                ordered_names.append(name)
                remaining.pop(name)
                for deps in remaining.values():
                    deps.discard(name)

        ordered_modules = tuple(by_name[name] for name in ordered_names)
        ordered_manifests = tuple(manifest_by_name[name] for name in ordered_names)
        required_timeframes: list[str] = []
        for manifest in ordered_manifests:
            for timeframe in (
                *manifest.required_timeframes,
                *manifest.warmup_bars.keys(),
            ):
                if timeframe != base_timeframe and timeframe not in required_timeframes:
                    required_timeframes.append(timeframe)
        return DependencyPlan(
            modules=ordered_modules,
            manifests=ordered_manifests,
            produced_by=produced_by,
            required_timeframes=tuple(required_timeframes),
        )

    def _validate_api(self, manifest: ModuleManifest) -> None:
        try:
            major = int(manifest.api_version.split(".", 1)[0])
        except (ValueError, IndexError) as exc:
            raise ModuleCompatibilityError(
                f"invalid API version for {manifest.name}: {manifest.api_version}"
            ) from exc
        if major != self.supported_api_major:
            raise ModuleCompatibilityError(
                f"module {manifest.name} requires API {manifest.api_version}; "
                f"kernel supports {self.supported_api_major}.x"
            )

    @staticmethod
    def _instantiate(target: Any, parameters: Mapping[str, Any]) -> Any:
        from .module_api import StrategyModule

        if isinstance(target, StrategyModule):
            if parameters:
                raise ModuleConfigurationError("parameters cannot be passed to a module instance")
            module = target
        elif inspect.isclass(target) or callable(target):
            try:
                module = target(**dict(parameters))
            except TypeError as exc:
                raise ModuleConfigurationError(
                    f"cannot instantiate {target!r} with parameters {dict(parameters)!r}: {exc}"
                ) from exc
        else:
            if parameters:
                raise ModuleConfigurationError("parameters cannot be passed to a module instance")
            module = target
        if not isinstance(module, StrategyModule):
            raise ModuleConfigurationError("loaded module must implement StrategyModule")
        manifest = manifest_for(module)
        if module.name != manifest.name:
            raise ModuleConfigurationError(
                f"module.name {module.name!r} does not match manifest.name {manifest.name!r}"
            )
        return module


def manifest_for(module: Any) -> ModuleManifest:
    manifest = getattr(module, "manifest", None)
    if manifest is None:
        return ModuleManifest(name=getattr(module, "name", ""))
    if callable(manifest) and not isinstance(manifest, ModuleManifest):
        manifest = manifest()
    if not isinstance(manifest, ModuleManifest):
        raise ModuleCompatibilityError("module.manifest must be ModuleManifest")
    return manifest


def parameters_for(module: Any) -> Mapping[str, Any]:
    parameters = getattr(module, "parameters", None)
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise ModuleConfigurationError("module.parameters must be a mapping")
    return _freeze_mapping(parameters, "module parameters")


def validate_parameters(manifest: ModuleManifest, parameters: Mapping[str, Any]) -> None:
    schema = manifest.parameters_schema
    unknown = sorted(set(parameters) - set(schema))
    if unknown:
        raise ModuleConfigurationError(
            f"unknown parameters for {manifest.name}: {', '.join(unknown)}"
        )
    for name, rules in schema.items():
        required = bool(rules.get("required", False))
        if name not in parameters:
            if required and "default" not in rules:
                raise ModuleConfigurationError(f"missing required parameter {manifest.name}.{name}")
            continue
        value = parameters[name]
        expected = rules.get("type")
        if expected and not _matches_type(value, str(expected)):
            raise ModuleConfigurationError(f"parameter {manifest.name}.{name} must be {expected}")
        if expected in {"number", "integer"} and isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ModuleConfigurationError(f"parameter {manifest.name}.{name} must be finite")
        if "minimum" in rules and value < rules["minimum"]:
            raise ModuleConfigurationError(
                f"parameter {manifest.name}.{name} below minimum {rules['minimum']}"
            )
        if "maximum" in rules and value > rules["maximum"]:
            raise ModuleConfigurationError(
                f"parameter {manifest.name}.{name} above maximum {rules['maximum']}"
            )
        if "enum" in rules and value not in rules["enum"]:
            raise ModuleConfigurationError(
                f"parameter {manifest.name}.{name} must be one of {tuple(rules['enum'])}"
            )


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "object":
        return isinstance(value, Mapping)
    return True


def _validate_parameter_schema(schema: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(schema, Mapping):
        raise TypeError("parameters_schema must be a mapping")
    for name, rules in schema.items():
        require_nonblank(name, "parameter name")
        if name != name.strip():
            raise ValueError("parameter name cannot contain surrounding whitespace")
        if not isinstance(rules, Mapping):
            raise TypeError(f"parameter schema {name} must be a mapping")
        unknown_rules = sorted(set(rules) - _PARAMETER_RULE_KEYS)
        if unknown_rules:
            raise ValueError(
                f"unknown parameter schema rules for {name}: {', '.join(unknown_rules)}"
            )
        expected = rules.get("type")
        if expected is not None:
            if not isinstance(expected, str) or expected not in _PARAMETER_TYPES:
                raise ValueError(f"unsupported parameter type for {name}: {expected!r}")
        required = rules.get("required")
        if required is not None and not isinstance(required, bool):
            raise TypeError(f"parameter schema {name}.required must be bool")
        minimum = rules.get("minimum")
        maximum = rules.get("maximum")
        for label, bound in (("minimum", minimum), ("maximum", maximum)):
            if bound is not None:
                if not isinstance(bound, (int, float)) or isinstance(bound, bool):
                    raise TypeError(f"parameter schema {name}.{label} must be numeric")
                if not math.isfinite(float(bound)):
                    raise ValueError(f"parameter schema {name}.{label} must be finite")
                if expected not in {"number", "integer"}:
                    raise ValueError(f"parameter schema {name}.{label} requires numeric type")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"parameter schema {name} minimum cannot exceed maximum")
        enum = rules.get("enum")
        if enum is not None:
            if not isinstance(enum, (list, tuple)) or not enum:
                raise ValueError(f"parameter schema {name}.enum must be a nonempty array")
            if any(
                any(item == previous for previous in enum[:index])
                for index, item in enumerate(enum)
            ):
                raise ValueError(f"parameter schema {name} enum cannot contain duplicates")
            if expected is not None and any(not _matches_type(item, expected) for item in enum):
                raise ValueError(f"parameter schema {name} enum values must be {expected}")
        if "default" not in rules:
            continue
        default = rules["default"]
        if expected is not None and not _matches_type(default, expected):
            raise ValueError(f"parameter schema {name} default must be {expected}")
        if expected in {"number", "integer"} and not math.isfinite(float(default)):
            raise ValueError(f"parameter schema {name} default must be finite")
        if minimum is not None and default < minimum:
            raise ValueError(f"parameter schema {name} default below minimum")
        if maximum is not None and default > maximum:
            raise ValueError(f"parameter schema {name} default above maximum")
        if enum is not None and default not in enum:
            raise ValueError(f"parameter schema {name} default must be one of {tuple(enum)}")


def _read_bounded_regular_text(path: Path, max_bytes: int | None) -> str:
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise ModuleConfigurationError(f"cannot stat module config: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise ModuleConfigurationError("module config cannot be a symlink")
    if not stat.S_ISREG(path_stat.st_mode):
        raise ModuleConfigurationError("module config must be a regular file")
    if max_bytes is not None and path_stat.st_size > max_bytes:
        raise ModuleConfigurationError("module config exceeds size limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModuleConfigurationError("cannot safely open module config") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ModuleConfigurationError("module config must be a regular file")
        if (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise ModuleConfigurationError("module config changed during open")
        chunks: list[bytes] = []
        total = 0
        while True:
            read_size = 64 * 1024
            if max_bytes is not None:
                read_size = min(read_size, max_bytes + 1 - total)
                if read_size <= 0:
                    raise ModuleConfigurationError("module config exceeds size limit")
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ModuleConfigurationError("module config exceeds size limit")
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModuleConfigurationError("module config must be valid UTF-8") from exc


def _safe_load_yaml(text: str, max_nodes: int) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise ModuleConfigurationError("PyYAML is required to load YAML module configs") from exc

    class CappedSafeLoader(yaml.SafeLoader):
        def __init__(self, stream: str) -> None:
            super().__init__(stream)
            self._composed_nodes = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            self._composed_nodes += 1
            if self._composed_nodes > max_nodes:
                raise yaml.YAMLError("module config exceeds YAML node limit")
            return super().compose_node(parent, index)

    try:
        loader = CappedSafeLoader(text)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        raise ModuleConfigurationError(f"invalid module YAML: {exc}") from exc


def _strict_string_mapping(value: Mapping[Any, Any], context: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ModuleConfigurationError(f"{context} key must be str")
        result[key] = item
    return result


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    require_nonblank(value, name)
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    return value


def _plain_tuple(value: object, name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    return tuple(value)


def _typed_tuple(value: object, expected: type[Any], name: str) -> tuple[Any, ...]:
    result = _plain_tuple(value, name)
    if any(not isinstance(item, expected) for item in result):
        raise TypeError(f"{name} must contain {expected.__name__}")
    return result


def _canonical_string_tuple(value: object, name: str) -> tuple[str, ...]:
    return tuple(_canonical_string(item, name) for item in _plain_tuple(value, name))


def _freeze_value(value: Any, context: str) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, context)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item, context) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item, context) for item in value)
    return value


def _freeze_mapping(value: Mapping[Any, Any], context: str) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = _canonical_string(raw_key, f"{context} key")
        frozen[key] = _freeze_value(item, f"{context}.{key}")
    return MappingProxyType(frozen)


def _validate_feature_name(value: str) -> None:
    require_nonblank(value, "feature name")
    parts = value.split(".")
    if len(parts) < 2 or any(not part.strip() for part in parts):
        raise ValueError(f"feature names must be namespaced: {value!r}")
