from __future__ import annotations

from importlib import util

from modular_control_plane import server_launcher


def test_api_launcher_reports_interface_install_command_when_extra_is_missing(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        util,
        "find_spec",
        lambda name: None if name == "uvicorn" else object(),
    )

    assert server_launcher.main(["--help"]) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert "optional 'interface' dependencies" in output.err
    assert "missing: uvicorn" in output.err
    assert "python -m pip install 'modular-control-plane[interface]'" in output.err
    assert (
        "python -m pip install '/path/to/modular_control_plane-<version>-py3-none-any.whl[interface]'"
        in output.err
    )
    assert "python -m pip install -e 'applications/control_plane[interface]'" in output.err
