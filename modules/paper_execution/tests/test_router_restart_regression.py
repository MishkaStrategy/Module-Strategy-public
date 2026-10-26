from __future__ import annotations

from decimal import Decimal

from paper_execution import (
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    RouterExecutionAdapter,
    StateVersions,
)
from test_router_cross_contract import (
    NOW,
    MutableOCCProvider,
    StaticMetadataProvider,
    _open_envelope,
)


def test_router_evidence_reloads_from_durable_journal_after_restart(
    versions,
    broker,
    metadata,
) -> None:
    envelope = _open_envelope()
    provider = MutableOCCProvider(envelope)
    config = PaperExecutionConfig()
    coordinator = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="router-restart-regression",
    )
    first = RouterExecutionAdapter(coordinator, occ_provider=provider).accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )
    snapshot = coordinator.snapshot_state()

    restored = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="router-restart-regression",
        event_store=coordinator.journal.store,
    )
    restored.restore_state(snapshot)
    second = RouterExecutionAdapter(restored, occ_provider=provider).accept(
        envelope,
        now=NOW,
        current_index=1,
        mark_price=Decimal("101"),
        paper_versions=StateVersions(portfolio=99, positions=99, orders=99),
    )

    assert first.accepted
    assert second.plan_id == first.plan_id
    assert provider.calls == 1
    assert len(broker.orders) == 1
