from __future__ import annotations

from market_data_ingestion.enums import ValidationStatus
from market_data_ingestion.funding_dataset_reader import (
    _CONSUMABLE_VALIDATION_STATUSES as FUNDING_CONSUMABLE_STATUSES,
)
from market_data_ingestion.price_dataset_reader import (
    _CONSUMABLE_VALIDATION_STATUSES as PRICE_CONSUMABLE_STATUSES,
)

_EXPECTED = frozenset((ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS))


def test_registered_readers_admit_exact_valid_status_set() -> None:
    assert FUNDING_CONSUMABLE_STATUSES == _EXPECTED
    assert PRICE_CONSUMABLE_STATUSES == _EXPECTED
    assert ValidationStatus.INVALID not in FUNDING_CONSUMABLE_STATUSES
    assert ValidationStatus.INVALID not in PRICE_CONSUMABLE_STATUSES
