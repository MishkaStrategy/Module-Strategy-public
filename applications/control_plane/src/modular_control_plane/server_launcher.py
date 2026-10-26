from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence

_INTERFACE_MODULES = ("fastapi", "httpx", "psutil", "uvicorn")
_INDEX_INSTALL_COMMAND = "python -m pip install 'modular-control-plane[interface]'"
_LOCAL_WHEEL_INSTALL_COMMAND = (
    "python -m pip install '/path/to/modular_control_plane-<version>-py3-none-any.whl[interface]'"
)
_EDITABLE_INSTALL_COMMAND = "python -m pip install -e 'applications/control_plane[interface]'"


def main(argv: Sequence[str] | None = None) -> int:
    missing = tuple(
        module for module in _INTERFACE_MODULES if importlib.util.find_spec(module) is None
    )
    if missing:
        print(
            "The Control Plane API needs the optional 'interface' dependencies "
            f"(missing: {', '.join(missing)}). For a package installed from an index, run:\n"
            f"  {_INDEX_INSTALL_COMMAND}\n"
            "If you installed a local wheel, reinstall that same wheel with the '[interface]' extra:\n"
            f"  {_LOCAL_WHEEL_INSTALL_COMMAND}\n"
            "Or, from the repository root, install the editable package with:\n"
            f"  {_EDITABLE_INSTALL_COMMAND}",
            file=sys.stderr,
        )
        return 2

    from .server import main as server_main

    return server_main(argv)
