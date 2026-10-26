"""Durable composition, evidence, approval and checkpoint ledger."""

from .governance_store_verify import GovernanceLedgerVerifyMixin
from .governance_store_write import GovernanceLedgerWriteMixin


class GovernanceLedger(GovernanceLedgerWriteMixin, GovernanceLedgerVerifyMixin):
    """Append-only governance state with restart and integrity verification."""


__all__ = ["GovernanceLedger"]
