from __future__ import annotations

from dataclasses import dataclass

from ._utils import canonical_json, sha256_text
from .enums import ExecutionMode, ReconciliationMode
from .exceptions import LiveExecutionNotSupported

MAX_CONFIGURATION_ORDER_LIMIT = 1_000_000
MAX_CLIENT_ORDER_ID_LENGTH = 128
MAX_MODULE_NAMESPACE_LENGTH = 32


@dataclass(frozen=True, slots=True)
class PaperExecutionConfig:
    execution_mode: ExecutionMode | str = ExecutionMode.PAPER
    schema_version: str = "1"
    reconciliation_mode: ReconciliationMode = ReconciliationMode.STRICT_BLOCK
    strict_invariants: bool = True
    maximum_open_orders: int = 100
    maximum_conditional_orders: int = 100
    client_order_id_max_length: int = 36
    module_namespace: str = "pevr"

    def __post_init__(self) -> None:
        try:
            mode = (
                self.execution_mode
                if isinstance(self.execution_mode, ExecutionMode)
                else ExecutionMode(self.execution_mode)
            )
        except (TypeError, ValueError) as exc:
            raise LiveExecutionNotSupported() from exc
        if mode is not ExecutionMode.PAPER:
            raise LiveExecutionNotSupported()
        object.__setattr__(self, "execution_mode", mode)
        try:
            reconciliation_mode = (
                self.reconciliation_mode
                if isinstance(self.reconciliation_mode, ReconciliationMode)
                else ReconciliationMode(self.reconciliation_mode)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported reconciliation_mode") from exc
        object.__setattr__(self, "reconciliation_mode", reconciliation_mode)
        if not isinstance(self.schema_version, str):
            raise TypeError("schema_version must be a string")
        if self.schema_version != "1":
            raise ValueError("unsupported configuration schema_version")
        if not isinstance(self.strict_invariants, bool):
            raise TypeError("strict_invariants must be bool")
        for name in (
            "maximum_open_orders",
            "maximum_conditional_orders",
            "client_order_id_max_length",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("maximum_open_orders", "maximum_conditional_orders"):
            if getattr(self, name) > MAX_CONFIGURATION_ORDER_LIMIT:
                raise ValueError(f"{name} exceeds supported limit")
        if self.client_order_id_max_length < 20:
            raise ValueError("client_order_id_max_length is too small for collision-resistant IDs")
        if self.client_order_id_max_length > MAX_CLIENT_ORDER_ID_LENGTH:
            raise ValueError("client_order_id_max_length exceeds supported limit")
        if not isinstance(self.module_namespace, str):
            raise TypeError("module_namespace must be a string")
        if not self.module_namespace or (len(self.module_namespace) > MAX_MODULE_NAMESPACE_LENGTH):
            raise ValueError("module_namespace has invalid length")
        if (
            not self.module_namespace.isascii()
            or not self.module_namespace.replace("-", "").isalnum()
        ):
            raise ValueError("module_namespace must contain only ASCII alphanumerics and hyphens")

    @property
    def canonical_hash(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "execution_mode": self.execution_mode,
                    "schema_version": self.schema_version,
                    "reconciliation_mode": self.reconciliation_mode,
                    "strict_invariants": self.strict_invariants,
                    "maximum_open_orders": self.maximum_open_orders,
                    "maximum_conditional_orders": (self.maximum_conditional_orders),
                    "client_order_id_max_length": (self.client_order_id_max_length),
                    "module_namespace": self.module_namespace,
                }
            )
        )
