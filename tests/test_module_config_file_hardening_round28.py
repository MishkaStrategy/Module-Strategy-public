from pathlib import Path

import pytest
import yaml

from modular_core import ModuleRegistry
from modular_core.manifest import ModuleConfigurationError


def test_yaml_loader_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "modules.yml"
    target.write_text("modules: []\n", encoding="utf-8")
    link = tmp_path / "link.yml"
    link.symlink_to(target)
    with pytest.raises(ModuleConfigurationError, match="symlink"):
        ModuleRegistry().load_config(link)

    oversized = tmp_path / "large.yml"
    oversized.write_text("modules: []\n" + "#" * 64, encoding="utf-8")
    with pytest.raises(ModuleConfigurationError, match="size limit"):
        ModuleRegistry(max_config_bytes=16).load_config(oversized)


def test_yaml_loader_requires_utf8_and_strict_class_plugin_types(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "binary.yml"
    binary.write_bytes(b"modules: []\n\xff")
    with pytest.raises(ModuleConfigurationError, match="UTF-8"):
        ModuleRegistry().load_config(binary)

    with pytest.raises(ModuleConfigurationError, match=r"modules\[0\]\.class must be str"):
        ModuleRegistry().load_config({"modules": [{"class": 123}]})
    registry = ModuleRegistry()
    registry.register("known", object())
    with pytest.raises(ModuleConfigurationError, match=r"modules\[0\]\.plugin must be str"):
        registry.load_config({"modules": [{"plugin": 123}]})


def test_loader_rejects_non_string_keys_and_yaml_node_excess(tmp_path: Path) -> None:
    with pytest.raises(ModuleConfigurationError, match="module config root key must be str"):
        ModuleRegistry().load_config({1: []})  # type: ignore[dict-item]
    with pytest.raises(ModuleConfigurationError, match=r"modules\[0\] key must be str"):
        ModuleRegistry().load_config({"modules": [{1: "bad"}]})  # type: ignore[dict-item]

    many_nodes = tmp_path / "nodes.yml"
    many_nodes.write_text("modules:\n  - enabled: false\n", encoding="utf-8")
    with pytest.raises(ModuleConfigurationError, match="node limit"):
        ModuleRegistry(max_yaml_nodes=2).load_config(many_nodes)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_yaml_nodes": True}, "max_yaml_nodes must be int"),
        ({"max_yaml_nodes": 0}, "max_yaml_nodes must be positive"),
    ],
)
def test_yaml_node_limit_type_is_strict(kwargs, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        ModuleRegistry(**kwargs)


def test_yaml_loader_rejects_unsafe_python_object_tag(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.yml"
    unsafe.write_text(
        "value: !!python/object/apply:builtins.str ['unsafe']\n",
        encoding="utf-8",
    )
    with pytest.raises(ModuleConfigurationError, match="invalid module YAML"):
        ModuleRegistry().load_config(unsafe)


def test_yaml_loader_disposes_custom_safe_loader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    disposed: list[str] = []
    original_dispose = yaml.SafeLoader.dispose

    def tracked_dispose(loader: yaml.SafeLoader) -> None:
        disposed.append(type(loader).__name__)
        original_dispose(loader)

    monkeypatch.setattr(yaml.SafeLoader, "dispose", tracked_dispose)
    config = tmp_path / "modules.yml"
    config.write_text("modules: []\n", encoding="utf-8")
    ModuleRegistry().load_config(config)
    assert disposed == ["CappedSafeLoader"]
