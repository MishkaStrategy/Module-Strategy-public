from __future__ import annotations


def test_public_import_surface_is_available() -> None:
    from modular_results_registry import (
        JsonLineLogger,
        OperationSample,
        OperationalMetrics,
        SQLiteRegistry,
        StructuredLogEvent,
        build_log_event,
        canonical_sha256,
    )

    assert SQLiteRegistry is not None
    assert JsonLineLogger is not None
    assert StructuredLogEvent is not None
    assert OperationSample is not None
    assert OperationalMetrics is not None
    assert callable(build_log_event)
    assert callable(canonical_sha256)
