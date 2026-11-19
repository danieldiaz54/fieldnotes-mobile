"""Pure typed domain exceptions for fieldnotes-mobile.

Every invariant violation, missing entity, optimistic concurrency conflict,
invalid conflict resolution, or offline synchronization failure raises an
explicit subclass of ``DomainError``. Zero external dependencies are permitted.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for all explicit domain exceptions."""

    code: str = "ERR-500-DOMAIN-ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message: str = message
        self.code: str = code if code is not None else self.__class__.code
        self.details: dict[str, Any] = dict(details) if details is not None else {}
        super().__init__(message)


class InvalidFieldNoteError(DomainError):
    """Raised when a field note, coordinate, outbox record, or clock violates invariants."""

    code: str = "ERR-400-INVALID-FIELD-NOTE"


class NoteNotFoundError(DomainError):
    """Raised when an operation references a field note that does not exist."""

    code: str = "ERR-404-NOTE-NOT-FOUND"

    def __init__(self, note_id: str, message: str | None = None) -> None:
        self.note_id: str = note_id
        resolved_message = message or f"Field note '{note_id}' was not found."
        super().__init__(
            resolved_message,
            code=self.code,
            details={"note_id": note_id},
        )


class SyncConflictError(DomainError):
    """Raised when optimistic concurrency detects version divergence during push."""

    code: str = "ERR-409-SYNC-CONFLICT"

    def __init__(
        self,
        note_id: str,
        local_version: int,
        server_version: int,
        message: str | None = None,
    ) -> None:
        self.note_id: str = note_id
        self.local_version: int = local_version
        self.server_version: int = server_version
        resolved_message = (
            message
            or f"Sync conflict detected for field note '{note_id}': "
            f"local_version={local_version} diverges from server_version={server_version}."
        )
        super().__init__(
            resolved_message,
            code=self.code,
            details={
                "note_id": note_id,
                "local_version": local_version,
                "server_version": server_version,
            },
        )


class InvalidConflictResolutionError(DomainError):
    """Raised when a conflict resolution request is invalid, incomplete, or mismatched."""

    code: str = "ERR-422-INVALID-RESOLUTION"


class OfflineNetworkError(DomainError):
    """Raised when remote synchronization is attempted while the device is offline."""

    code: str = "ERR-503-DEVICE-OFFLINE"

    def __init__(
        self,
        message: str = (
            "Device is currently offline; mutation remains queued in the local outbox."
        ),
        *,
        device_id: str | None = None,
    ) -> None:
        self.device_id: str | None = device_id
        details: dict[str, Any] = {}
        if device_id is not None:
            details["device_id"] = device_id
        super().__init__(message, code=self.code, details=details)


__all__ = [
    "DomainError",
    "InvalidConflictResolutionError",
    "InvalidFieldNoteError",
    "NoteNotFoundError",
    "OfflineNetworkError",
    "SyncConflictError",
]
