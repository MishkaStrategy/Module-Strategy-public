"""Public SQLite registry assembled from bounded implementation mixins.

The small set of re-exported stdlib modules and private resource-limit constants
preserves the existing test/failure-injection seam while the implementation is
split across focused mixins.
"""

from .storage_common import (
    RegisterOutcome,
    _MAX_REGISTRATION_ENTITIES,
    _MAX_REGISTRATION_PACKAGE_METADATA_BYTES,
    _sql_identifier,
)
from .storage_integrity import StorageIntegrityMixin
from .storage_integrity_audit import StorageIntegrityAuditMixin
from .storage_integrity_records import StorageIntegrityRecordsMixin
from .storage_lifecycle import StorageLifecycleMixin
from .storage_query import StorageQueryMixin
from .storage_rebuild import StorageRebuildMixin
from .storage_registration import StorageRegistrationMixin
from .storage_registration_evidence import StorageRegistrationEvidenceMixin
from .storage_registration_metrics import StorageRegistrationMetricsMixin
from .storage_registration_primary import StorageRegistrationPrimaryMixin
from .storage_registration_runs import StorageRegistrationRunsMixin
from .storage_registration_support import StorageRegistrationSupportMixin
from .storage_schema import StorageSchemaMixin


class SQLiteRegistry(
    StorageLifecycleMixin,
    StorageSchemaMixin,
    StorageRegistrationSupportMixin,
    StorageRegistrationPrimaryMixin,
    StorageRegistrationRunsMixin,
    StorageRegistrationMetricsMixin,
    StorageRegistrationEvidenceMixin,
    StorageRegistrationMixin,
    StorageQueryMixin,
    StorageRebuildMixin,
    StorageIntegrityRecordsMixin,
    StorageIntegrityAuditMixin,
    StorageIntegrityMixin,
):
    """Transactional, immutable, append-audited local registry metadata store."""


__all__ = [
    "RegisterOutcome",
    "SQLiteRegistry",
    "_MAX_REGISTRATION_ENTITIES",
    "_MAX_REGISTRATION_PACKAGE_METADATA_BYTES",
    "_sql_identifier",
]
