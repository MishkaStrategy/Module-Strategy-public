from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StructuredIngestionError(Exception):
    code: str
    message: str
    retryable: bool = False
    stage: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("error code must be non-empty")
        if not self.message.strip():
            raise ValueError("error message must be non-empty")
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "stage": self.stage,
            "details": dict(self.details),
        }


class ContractError(StructuredIngestionError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("CONTRACT_ERROR", message, False, "validation", details or {})


class AdapterError(StructuredIngestionError):
    pass


class StorageError(StructuredIngestionError):
    pass


class RegistryError(StructuredIngestionError):
    pass
