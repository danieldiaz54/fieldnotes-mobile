"""Pure Domain Layer for FieldNotes Mobile Offline-First Sync Engine."""

from src.domain.exceptions import (
    DomainError,
    InvalidConflictResolutionError,
    InvalidFieldNoteError,
    NoteNotFoundError,
    OfflineNetworkError,
    SyncConflictError,
)
from src.domain.notes import (
    ConflictStrategy,
    FieldNote,
    GeoCoordinate,
    InspectionSeverity,
    SyncState,
)
from src.domain.repositories import (
    LocalDeviceStoreProtocol,
    RemoteSyncStoreProtocol,
)
from src.domain.sync_engine import (
    ConflictRecord,
    ConflictResolver,
    LamportClock,
    MutationOperation,
    MutationStatus,
    PendingMutation,
    SyncAuditRecord,
)

__all__ = [
    "ConflictRecord",
    "ConflictResolver",
    "ConflictStrategy",
    "DomainError",
    "FieldNote",
    "GeoCoordinate",
    "InspectionSeverity",
    "InvalidConflictResolutionError",
    "InvalidFieldNoteError",
    "LamportClock",
    "LocalDeviceStoreProtocol",
    "MutationOperation",
    "MutationStatus",
    "NoteNotFoundError",
    "OfflineNetworkError",
    "PendingMutation",
    "RemoteSyncStoreProtocol",
    "SyncAuditRecord",
    "SyncConflictError",
    "SyncState",
]
