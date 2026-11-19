"""Offline-first synchronization outbox models, Lamport logical clock, and conflict resolver.

Zero external or infrastructure dependencies: uses Python 3.12 standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import threading

from src.domain.exceptions import (
    InvalidConflictResolutionError,
    InvalidFieldNoteError,
    SyncConflictError,
)
from src.domain.notes import ConflictStrategy, FieldNote, SyncState


class MutationOperation(str, Enum):
    """Mutation action persisted in the local device outbox queue."""

    UPSERT = "UPSERT"
    DELETE = "DELETE"


class MutationStatus(str, Enum):
    """Delivery status of a queued outbox mutation."""

    QUEUED = "QUEUED"
    SYNCED = "SYNCED"
    CONFLICT = "CONFLICT"


def _require_non_empty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidFieldNoteError(f"{field_name} must be a non-empty string.")
    return value


def _require_int_at_least(value: object, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidFieldNoteError(
            f"{field_name} must be a non-bool integer >= {minimum}; got {value!r}."
        )
    return value


def _coerce_operation(value: object) -> MutationOperation:
    if isinstance(value, MutationOperation):
        return value
    if isinstance(value, str):
        try:
            return MutationOperation(value)
        except ValueError as exc:
            raise InvalidFieldNoteError(
                f"PendingMutation.operation must be a valid MutationOperation; got {value!r}."
            ) from exc
    raise InvalidFieldNoteError(
        f"PendingMutation.operation must be a MutationOperation; got {value!r}."
    )


def _coerce_mutation_status(value: object) -> MutationStatus:
    if isinstance(value, MutationStatus):
        return value
    if isinstance(value, str):
        try:
            return MutationStatus(value)
        except ValueError as exc:
            raise InvalidFieldNoteError(
                f"PendingMutation.status must be a valid MutationStatus; got {value!r}."
            ) from exc
    raise InvalidFieldNoteError(
        f"PendingMutation.status must be a MutationStatus; got {value!r}."
    )


def _coerce_conflict_strategy(value: object) -> ConflictStrategy:
    if isinstance(value, ConflictStrategy):
        return value
    if isinstance(value, str):
        try:
            return ConflictStrategy(value)
        except ValueError as exc:
            raise InvalidConflictResolutionError(
                f"Unsupported conflict resolution strategy: {value!r}."
            ) from exc
    raise InvalidConflictResolutionError(
        f"Conflict resolution strategy must be a ConflictStrategy or string; got {value!r}."
    )


@dataclass(frozen=True)
class PendingMutation:
    """Immutable durable outbox entry representing a pending local mutation."""

    mutation_id: str
    note_id: str
    device_id: str
    operation: MutationOperation
    base_version: int
    lamport_ts: int
    payload_json: str
    status: MutationStatus
    created_at: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.mutation_id, "PendingMutation.mutation_id")
        _require_non_empty_str(self.note_id, "PendingMutation.note_id")
        _require_non_empty_str(self.device_id, "PendingMutation.device_id")

        object.__setattr__(self, "operation", _coerce_operation(self.operation))
        _require_int_at_least(self.base_version, "PendingMutation.base_version", minimum=0)
        _require_int_at_least(self.lamport_ts, "PendingMutation.lamport_ts", minimum=1)
        _require_non_empty_str(self.payload_json, "PendingMutation.payload_json")
        object.__setattr__(self, "status", _coerce_mutation_status(self.status))

        if not isinstance(self.created_at, str):
            raise InvalidFieldNoteError(
                f"PendingMutation.created_at must be a string; got {type(self.created_at).__name__}."
            )


@dataclass(frozen=True)
class ConflictRecord:
    """Immutable record preserving diverging local and remote versions of a field note."""

    conflict_id: str
    note_id: str
    local_note: FieldNote
    remote_note: FieldNote
    detected_at: str
    resolved: bool = False
    strategy: ConflictStrategy | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.conflict_id, "ConflictRecord.conflict_id")
        _require_non_empty_str(self.note_id, "ConflictRecord.note_id")

        if not isinstance(self.local_note, FieldNote):
            raise InvalidFieldNoteError(
                f"ConflictRecord.local_note must be a FieldNote; got {type(self.local_note).__name__}."
            )
        if not isinstance(self.remote_note, FieldNote):
            raise InvalidFieldNoteError(
                f"ConflictRecord.remote_note must be a FieldNote; got {type(self.remote_note).__name__}."
            )
        if self.local_note.note_id != self.note_id or self.remote_note.note_id != self.note_id:
            raise InvalidConflictResolutionError(
                f"ConflictRecord note_id '{self.note_id}' must match local_note.note_id "
                f"('{self.local_note.note_id}') and remote_note.note_id "
                f"('{self.remote_note.note_id}')."
            )
        if not isinstance(self.detected_at, str):
            raise InvalidFieldNoteError(
                f"ConflictRecord.detected_at must be a string; got {type(self.detected_at).__name__}."
            )
        if not isinstance(self.resolved, bool):
            raise InvalidFieldNoteError(
                f"ConflictRecord.resolved must be a bool; got {type(self.resolved).__name__}."
            )

        coerced_strategy: ConflictStrategy | None = None
        if self.strategy is not None:
            coerced_strategy = _coerce_conflict_strategy(self.strategy)
            object.__setattr__(self, "strategy", coerced_strategy)

        if self.resolved and coerced_strategy is None:
            raise InvalidConflictResolutionError(
                "A resolved ConflictRecord must specify a ConflictStrategy."
            )


@dataclass(frozen=True)
class SyncAuditRecord:
    """Immutable append-only audit entry capturing the outcome of a sync cycle."""

    audit_id: str
    device_id: str
    direction: str
    pushed_count: int
    pulled_count: int
    conflicts_detected: int
    status: str
    created_at: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.audit_id, "SyncAuditRecord.audit_id")
        _require_non_empty_str(self.device_id, "SyncAuditRecord.device_id")
        _require_non_empty_str(self.direction, "SyncAuditRecord.direction")
        _require_int_at_least(self.pushed_count, "SyncAuditRecord.pushed_count", minimum=0)
        _require_int_at_least(self.pulled_count, "SyncAuditRecord.pulled_count", minimum=0)
        _require_int_at_least(
            self.conflicts_detected,
            "SyncAuditRecord.conflicts_detected",
            minimum=0,
        )
        _require_non_empty_str(self.status, "SyncAuditRecord.status")
        if not isinstance(self.created_at, str):
            raise InvalidFieldNoteError(
                f"SyncAuditRecord.created_at must be a string; got {type(self.created_at).__name__}."
            )


class LamportClock:
    """Thread-safe Lamport logical clock for causal ordering across offline devices."""

    def __init__(self, initial: int = 0) -> None:
        validated_initial = _require_int_at_least(initial, "LamportClock.initial", minimum=0)
        self._lock = threading.Lock()
        self._counter: int = validated_initial

    def tick(self) -> int:
        """Advance local clock by 1 for a local mutation event and return the new timestamp."""
        with self._lock:
            self._counter += 1
            return self._counter

    def receive(self, remote_ts: int) -> int:
        """Merge a remote logical timestamp using max(local, remote) + 1."""
        validated_remote = _require_int_at_least(
            remote_ts,
            "LamportClock.remote_ts",
            minimum=0,
        )
        with self._lock:
            self._counter = max(self._counter, validated_remote) + 1
            return self._counter

    def current(self) -> int:
        """Return the current logical clock value without incrementing."""
        with self._lock:
            return self._counter

    def __repr__(self) -> str:
        return f"LamportClock(current={self.current()})"


class ConflictResolver:
    """Deterministic optimistic concurrency detector and conflict resolution engine."""

    @staticmethod
    def has_version_conflict(base_version: int, server_version: int) -> bool:
        """Return True when the local base version diverges from the server version."""
        _require_int_at_least(base_version, "base_version", minimum=0)
        _require_int_at_least(server_version, "server_version", minimum=0)
        return base_version != server_version

    @staticmethod
    def assert_no_version_conflict(
        note_id: str,
        base_version: int,
        server_version: int,
    ) -> None:
        """Raise SyncConflictError if base_version != server_version."""
        _require_non_empty_str(note_id, "note_id")
        if ConflictResolver.has_version_conflict(base_version, server_version):
            raise SyncConflictError(
                note_id=note_id,
                local_version=base_version,
                server_version=server_version,
            )

    @staticmethod
    def resolve(
        local_note: FieldNote,
        remote_note: FieldNote,
        strategy: ConflictStrategy | str,
        merged_title: str | None = None,
        merged_body: str | None = None,
        updated_at: str | None = None,
    ) -> FieldNote:
        """Deterministically resolve a conflict between local and remote versions of a note.

        Computes ``next_version = max(local_note.version, remote_note.version) + 1`` and
        ``next_lamport = max(local_note.lamport_ts, remote_note.lamport_ts) + 1`` so the
        converged note strictly dominates both conflicting lineages.
        """
        if not isinstance(local_note, FieldNote) or not isinstance(remote_note, FieldNote):
            raise InvalidConflictResolutionError(
                "Both local_note and remote_note must be FieldNote instances."
            )
        if local_note.note_id != remote_note.note_id:
            raise InvalidConflictResolutionError(
                f"Cannot resolve conflict across different note IDs: "
                f"local='{local_note.note_id}' vs remote='{remote_note.note_id}'."
            )

        resolved_strategy = _coerce_conflict_strategy(strategy)

        if updated_at is None:
            resolved_updated_at = datetime.now(timezone.utc).isoformat()
        elif isinstance(updated_at, str):
            resolved_updated_at = updated_at
        else:
            raise InvalidConflictResolutionError(
                f"updated_at must be a string or None; got {type(updated_at).__name__}."
            )

        next_version = max(local_note.version, remote_note.version) + 1
        next_lamport = max(local_note.lamport_ts, remote_note.lamport_ts) + 1

        if resolved_strategy is ConflictStrategy.KEEP_LOCAL:
            return FieldNote(
                note_id=local_note.note_id,
                site_code=local_note.site_code,
                title=local_note.title,
                body=local_note.body,
                severity=local_note.severity,
                location=local_note.location,
                author_device_id=local_note.author_device_id,
                version=next_version,
                lamport_ts=next_lamport,
                sync_state=SyncState.PENDING_PUSH,
                updated_at=resolved_updated_at,
                deleted=local_note.deleted,
            )

        if resolved_strategy is ConflictStrategy.ACCEPT_REMOTE:
            return FieldNote(
                note_id=remote_note.note_id,
                site_code=remote_note.site_code,
                title=remote_note.title,
                body=remote_note.body,
                severity=remote_note.severity,
                location=remote_note.location,
                author_device_id=remote_note.author_device_id,
                version=next_version,
                lamport_ts=next_lamport,
                sync_state=SyncState.PENDING_PUSH,
                updated_at=resolved_updated_at,
                deleted=remote_note.deleted,
            )

        # ConflictStrategy.MANUAL_MERGE
        if not isinstance(merged_title, str) or not merged_title.strip():
            raise InvalidConflictResolutionError(
                "MANUAL_MERGE strategy requires a non-empty 'merged_title'."
            )
        if not isinstance(merged_body, str) or not merged_body.strip():
            raise InvalidConflictResolutionError(
                "MANUAL_MERGE strategy requires a non-empty 'merged_body'."
            )

        return FieldNote(
            note_id=local_note.note_id,
            site_code=local_note.site_code,
            title=merged_title,
            body=merged_body,
            severity=local_note.severity,
            location=local_note.location,
            author_device_id=local_note.author_device_id,
            version=next_version,
            lamport_ts=next_lamport,
            sync_state=SyncState.PENDING_PUSH,
            updated_at=resolved_updated_at,
            deleted=False,
        )


__all__ = [
    "ConflictRecord",
    "ConflictResolver",
    "LamportClock",
    "MutationOperation",
    "MutationStatus",
    "PendingMutation",
    "SyncAuditRecord",
]
