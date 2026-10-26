from __future__ import annotations

from dataclasses import dataclass, field


_AuditItem = tuple[str, str, str, str]


@dataclass(slots=True)
class _RegistrationAccumulator:
    registered: int = 0
    duplicates: int = 0
    pending_audit: list[_AuditItem] = field(default_factory=list)

    def record(self, added: bool, audit_item: _AuditItem) -> None:
        if added:
            self.registered += 1
            self.pending_audit.append(audit_item)
        else:
            self.duplicates += 1
