"""Inverted persistence port protocols for local device storage and remote sync authority.

Zero external or infrastructure dependencies: uses Python 3.12 standard library only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from src.domain.notes import FieldNote
from src.domain.sync_engine import (
    ConflictRecord,
    MutationStatus,
    PendingMutation,
    SyncAuditRecord,
)


@runtime_checkable
class LocalDeviceStoreProtocol(Protocol):
    """Port interface for durable on-device storage (field notes, outbox, conflicts, audits)."""

    def upsert_note(self, note: FieldNote) -> None:
        """Persist or replace a FieldNote in local device storage."""
        ...

    def get_note(self, note_id: str) -> FieldNote | None:
        """Retrieve a local FieldNote by identifier, or None if absent."""
        ...

    def list_notes(self, *, include_deleted: bool = False) -> Sequence[FieldNote]:
        """Return local field notes in deterministic order."""
        ...

    def enqueue_mutation(self, mutation: PendingMutation) -> None:
        """Persist a PendingMutation into the durable local outbox queue."""
        ...

    def list_pending_mutations(
        self,
        *,
        status: MutationStatus | None = None,
    ) -> Sequence[PendingMutation]:
        """List outbox mutations ordered causally by (lamport_ts, created_at, mutation_id)."""
        ...

    def update_mutation_status(self, mutation_id: str, status: MutationStatus) -> None:
        """Transition a persisted outbox mutation to a new MutationStatus."""
        ...

    def save_conflict(self, conflict: ConflictRecord) -> None:
        """Insert or update a ConflictRecord in local storage."""
        ...

    def get_conflict(self, conflict_id: str) -> ConflictRecord | None:
        """Retrieve a ConflictRecord by identifier, or None if absent."""
        ...

    def list_conflicts(self, *, only_unresolved: bool = True) -> Sequence[ConflictRecord]:
        """Return conflict records, defaulting to unresolved conflicts."""
        ...

    def append_sync_audit(self, record: SyncAuditRecord) -> None:
        """Append an immutable SyncAuditRecord to the local sync audit log."""
        ...

    def list_sync_audits(self) -> Sequence[SyncAuditRecord]:
        """Return recorded sync audit entries in deterministic order."""
        ...

    def get_max_lamport_ts(self) -> int:
        """Return the highest persisted Lamport timestamp across local notes and outbox (0 if empty)."""
        ...


@runtime_checkable
class RemoteSyncStoreProtocol(Protocol):
    """Port interface for the authoritative remote synchronization backend."""

    def get_note(self, note_id: str) -> FieldNote | None:
        """Retrieve the authoritative remote FieldNote by identifier, or None if absent.

        Implementations may raise ``OfflineNetworkError`` when connectivity is unavailable.
        """
        ...

    def list_notes(
        self,
        *,
        since_lamport_ts: int = 0,
        include_deleted: bool = True,
    ) -> Sequence[FieldNote]:
        """Pull remote notes with lamport_ts > since_lamport_ts in deterministic causal order.

        Implementations may raise ``OfflineNetworkError`` when connectivity is unavailable.
        """
        ...

    def push_note(self, note: FieldNote, *, expected_base_version: int) -> FieldNote:
        """Atomically verify optimistic concurrency and persist a pushed FieldNote.

        Raises:
            OfflineNetworkError: If the device is currently offline.
            SyncConflictError: If ``expected_base_version`` diverges from the current server version.
        """
        ...


__all__ = [
    "LocalDeviceStoreProtocol",
    "RemoteSyncStoreProtocol",
]
