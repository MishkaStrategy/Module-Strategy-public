"""Compatibility facade for the verified Modular Core 0.4 export adapter."""

from .core_adapter_contract import CoreImportResult, CoreRunContext
from .core_adapter_import import adapt_core_v04_export

_MAX_JSON_BYTES = 64 * 1024 * 1024

__all__ = ["CoreImportResult", "CoreRunContext", "adapt_core_v04_export"]
