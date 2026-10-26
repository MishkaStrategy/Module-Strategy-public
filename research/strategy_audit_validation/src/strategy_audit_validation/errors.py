"""Explicit fail-closed error taxonomy."""

from __future__ import annotations


class StrategyAuditError(RuntimeError):
    """Base class for independent audit failures."""


class EvidenceMalformedError(StrategyAuditError):
    """Evidence cannot be decoded or violates the contract schema."""


class EvidenceIdentityConflictError(StrategyAuditError):
    """A stable evidence or decision identity was reused with different content."""


class EvidenceTamperedError(StrategyAuditError):
    """Evidence or durable audit-chain integrity does not verify."""


class AuditUnavailableError(StrategyAuditError):
    """A mandatory independent verifier or durable store is unavailable."""
