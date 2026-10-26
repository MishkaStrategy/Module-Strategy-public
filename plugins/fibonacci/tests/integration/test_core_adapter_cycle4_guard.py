from __future__ import annotations

from tests.integration.test_core_adapter import FakeModuleDecision
from tests.integration.test_core_adapter_cycle4 import (
    load_v4,
    module_with_permissive_regime,
    view_4h,
)


def test_v4_temporarily_disables_legacy_short_publication(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    observed: list[bool] = []

    def base_on_bar(module, _view):
        observed.append(module.publish_short_setup)
        return FakeModuleDecision(diagnostics={"short_setups_published": 0})

    monkeypatch.setattr(adapter._CoreAuditedFibonacciModule, "on_bar", base_on_bar)
    module = module_with_permissive_regime(adapter)
    module.on_bar(view_4h())

    assert observed == [False]
    assert module.publish_short_setup is True
