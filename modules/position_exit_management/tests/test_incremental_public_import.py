from __future__ import annotations


def test_direct_module_import_resolves_to_hardened_public_class():
    from modular_strategy_position_exit_management import (
        PositionExitManagementModule as package_class,
    )
    from modular_strategy_position_exit_management.module import (
        PositionExitManagementModule as direct_class,
    )

    assert direct_class is package_class
    assert direct_class.__module__ == "modular_strategy_position_exit_management.hardening"
