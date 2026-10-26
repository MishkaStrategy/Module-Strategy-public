from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_static_audit() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "static_audit.py"
    spec = importlib.util.spec_from_file_location("mdi_static_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_static_audit_accepts_annotated_safe_source(tmp_path: Path) -> None:
    module = _load_static_audit()
    source = tmp_path / "package"
    source.mkdir()
    (source / "safe.py").write_text(
        "def answer(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )

    report = module.audit(source, line_length=100)

    assert report["status"] == "ok"
    assert report["finding_count"] == 0


def test_static_audit_detects_unsafe_constructs(tmp_path: Path) -> None:
    module = _load_static_audit()
    source = tmp_path / "package"
    source.mkdir()
    (source / "unsafe.py").write_text(
        "import pickle\n"
        "def unsafe(values=[]):\n"
        "    assert values\n"
        "    try:\n"
        "        eval('1 + 1')\n"
        "    except:\n"
        "        pass\n",
        encoding="utf-8",
    )

    report = module.audit(source, line_length=100)
    codes = report["finding_counts"]

    assert report["status"] == "failed"
    assert codes["UNSAFE_SERIALIZATION_IMPORT"] == 1
    assert codes["RETURN_ANNOTATION_MISSING"] == 1
    assert codes["MUTABLE_DEFAULT"] == 1
    assert codes["RUNTIME_ASSERT"] == 1
    assert codes["DYNAMIC_CODE_EXECUTION"] == 1
    assert codes["BARE_EXCEPT"] == 1
