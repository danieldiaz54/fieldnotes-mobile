"""Application service orchestrating offline-first SQLite outbox sync and conflict resolution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import threading
from typing import Any
import uuid

from src.domain.exceptions import (
    InvalidConflictResolutionError,
    InvalidFieldNoteError,
    OfflineNetworkError,
)
from src.domain.notes import (
    ConflictStrategy,
    FieldNote,
    GeoCoordinate,
    InspectionSeverity,
    SyncState,
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
from src.infrastructure.persistence.seed_fieldnotes import seed_synced_stores
from src.infrastructure.persistence.sqlite_sync_stores import (
    SQLiteDeviceStore,
    SQLiteRemoteSyncServerStore,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mutation_to_dict(mutation: PendingMutation) -> dict[str, Any]:
    return {
        "mutation_id": mutation.mutation_id,
        "note_id": mutation.note_id,
        "device_id": mutation.device_id,
        "operation": mutation.operation.value,
        "base_version": mutation.base_version,
        "lamport_ts": mutation.lamport_ts,
        "payload_json": mutation.payload_json,
        "status": mutation.status.value,
        "created_at": mutation.created_at,
    }


def _conflict_to_dict(conflict: ConflictRecord) -> dict[str, Any]:
    return {
        "conflict_id": conflict.conflict_id,
        "note_id": conflict.note_id,
        "local_note": conflict.local_note.to_dict(),
        "remote_note": conflict.remote_note.to_dict(),
        "detected_at": conflict.detected_at,
        "resolved": conflict.resolved,
        "strategy": conflict.strategy.value if conflict.strategy is not None else None,
    }


def _audit_to_dict(record: SyncAuditRecord) -> dict[str, Any]:
    return {
        "audit_id": record.audit_id,
        "device_id": record.device_id,
        "direction": record.direction,
        "pushed_count": record.pushed_count,
        "pulled_count": record.pulled_count,
        "conflicts_detected": record.conflicts_detected,
        "status": record.status,
        "created_at": record.created_at,
    }


def _coerce_severity_input(value: object) -> InspectionSeverity:
    if isinstance(value, InspectionSeverity):
        return value
    if isinstance(value, str):
        try:
            return InspectionSeverity(value)
        except ValueError as exc:
            raise InvalidFieldNoteError(
                f"severity must be one of INFO, WARNING, CRITICAL; got {value!r}."
            ) from exc
    raise InvalidFieldNoteError(
        f"severity must be a valid InspectionSeverity string; got {value!r}."
    )


def _coerce_operation_input(value: object) -> MutationOperation:
    if isinstance(value, MutationOperation):
        return value
    if isinstance(value, str):
        try:
            return MutationOperation(value)
        except ValueError as exc:
            raise InvalidFieldNoteError(
                f"operation must be UPSERT or DELETE; got {value!r}."
            ) from exc
    raise InvalidFieldNoteError(
        f"operation must be a MutationOperation string; got {value!r}."
    )


class FieldNotesApplicationService:
    """Use-case orchestrator binding pure domain sync primitives to SQLite device & remote stores."""

    def __init__(
        self,
        device_store: SQLiteDeviceStore | None = None,
        remote_store: SQLiteRemoteSyncServerStore | None = None,
        *,
        device_db_path: str = ":memory:",
        remote_db_path: str = ":memory:",
        device_id: str = "device-ios-field-01",
        auto_seed: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self.device_store: SQLiteDeviceStore = (
            device_store
            if device_store is not None
            else SQLiteDeviceStore(db_path=device_db_path, device_id=device_id)
        )
        self.remote_store: SQLiteRemoteSyncServerStore = (
            remote_store
            if remote_store is not None
            else SQLiteRemoteSyncServerStore(db_path=remote_db_path)
        )
        self._owns_device_store = device_store is None
        self._owns_remote_store = remote_store is None

        if auto_seed and not self.device_store.list_local_notes() and not self.remote_store.list_remote_notes():
            seed_synced_stores(self.device_store, self.remote_store)

        self.clock: LamportClock = LamportClock(initial=self.device_store.get_max_lamport_ts())
        self.is_online: bool = True
        self._last_pull_lamport_ts: int = self.clock.current()

    def close(self) -> None:
        """Close owned SQLite store connections cleanly."""
        with self._lock:
            if self._owns_device_store:
                self.device_store.close()
            if self._owns_remote_store:
                self.remote_store.close()

    def get_health(self) -> dict[str, Any]:
        """Return operational status, connectivity, Lamport clock, and queue telemetry."""
        with self._lock:
            local_notes = self.device_store.list_local_notes()
            remote_notes = self.remote_store.list_remote_notes()
            queued_mutations = self.device_store.list_pending_mutations(only_queued=True)
            unresolved_conflicts = self.device_store.list_conflicts(unresolved_only=True)
            audits = self.device_store.list_sync_audits(limit=500)
            return {
                "status": "ok",
                "service": "fieldnotes-mobile",
                "device_id": self.device_store.device_id,
                "is_online": self.is_online,
                "lamport_ts": self.clock.current(),
                "last_pull_lamport_ts": self._last_pull_lamport_ts,
                "local_notes_count": len(local_notes),
                "remote_notes_count": len(remote_notes),
                "queued_mutations_count": len(queued_mutations),
                "unresolved_conflicts_count": len(unresolved_conflicts),
                "sync_audits_count": len(audits),
            }

    def set_connectivity(self, is_online: bool) -> dict[str, Any]:
        """Toggle simulated device network connectivity with strict boolean validation."""
        if not isinstance(is_online, bool):
            raise InvalidFieldNoteError(
                f"is_online must be a strict boolean (true or false); got {type(is_online).__name__}."
            )
        with self._lock:
            self.is_online = is_online
            queued = self.device_store.list_pending_mutations(only_queued=True)
            unresolved = self.device_store.list_conflicts(unresolved_only=True)
            return {
                "device_id": self.device_store.device_id,
                "is_online": self.is_online,
                "lamport_ts": self.clock.current(),
                "queued_mutations_count": len(queued),
                "unresolved_conflicts_count": len(unresolved),
            }

    def list_local_notes(self) -> list[dict[str, Any]]:
        """List all field notes persisted on the local mobile device store."""
        with self._lock:
            return [note.to_dict() for note in self.device_store.list_local_notes()]

    def list_remote_notes(self) -> list[dict[str, Any]]:
        """List all authoritative field notes stored on the remote sync server."""
        with self._lock:
            return [note.to_dict() for note in self.remote_store.list_remote_notes()]

    def upsert_local_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or update a local FieldNote offline or online, enqueueing an ACID outbox mutation."""
        if not isinstance(payload, Mapping):
            raise InvalidFieldNoteError("FieldNote payload must be a JSON object.")

        with self._lock:
            if "note_id" in payload and payload["note_id"] is not None:
                raw_note_id = payload["note_id"]
                if not isinstance(raw_note_id, str) or not raw_note_id.strip():
                    raise InvalidFieldNoteError("FieldNote.note_id must be a non-empty string.")
                note_id = raw_note_id
            else:
                note_id = f"NOTE-{uuid.uuid4().hex[:8].upper()}"

            existing = self.device_store.get_note(note_id)
            if existing is None:
                base_version = 0
                next_version = 1
            else:
                base_version = existing.version
                next_version = existing.version + 1

            site_code = (
                payload["site_code"]
                if "site_code" in payload
                else (existing.site_code if existing is not None else "")
            )
            title = (
                payload["title"]
                if "title" in payload
                else (existing.title if existing is not None else "")
            )
            body = (
                payload["body"]
                if "body" in payload
                else (existing.body if existing is not None else "")
            )
            severity = (
                payload["severity"]
                if "severity" in payload
                else (existing.severity if existing is not None else InspectionSeverity.INFO)
            )
            author_device_id = (
                payload["author_device_id"]
                if "author_device_id" in payload and payload["author_device_id"] is not None
                else (existing.author_device_id if existing is not None else self.device_store.device_id)
            )

            if "location" in payload:
                raw_location = payload["location"]
                location = (
                    raw_location
                    if isinstance(raw_location, GeoCoordinate)
                    else GeoCoordinate.from_dict(raw_location)
                )
            elif "latitude" in payload or "longitude" in payload:
                if "latitude" not in payload or "longitude" not in payload:
                    raise InvalidFieldNoteError(
                        "Both 'latitude' and 'longitude' are required when specifying coordinates."
                    )
                location = GeoCoordinate(
                    latitude=payload["latitude"],
                    longitude=payload["longitude"],
                )
            elif existing is not None:
                location = existing.location
            else:
                raise InvalidFieldNoteError(
                    "FieldNote payload must include 'location' with 'latitude' and 'longitude'."
                )

            deleted = (
                payload["deleted"]
                if "deleted" in payload
                else (existing.deleted if existing is not None else False)
            )
            if not isinstance(deleted, bool):
                raise InvalidFieldNoteError(
                    f"FieldNote.deleted must be a bool; got {type(deleted).__name__}."
                )

            if "operation" in payload and payload["operation"] is not None:
                operation = _coerce_operation_input(payload["operation"])
            else:
                operation = MutationOperation.DELETE if deleted else MutationOperation.UPSERT

            now_iso = _utc_now_iso()
            # Validate all domain invariants before advancing the Lamport clock.
            candidate_ts = self.clock.current() + 1
            validated_note = FieldNote(
                note_id=note_id,
                site_code=site_code,
                title=title,
                body=body,
                severity=severity,
                location=location,
                author_device_id=author_device_id,
                version=next_version,
                lamport_ts=candidate_ts,
                sync_state=SyncState.PENDING_PUSH,
                updated_at=now_iso,
                deleted=True if operation is MutationOperation.DELETE else deleted,
            )

            actual_ts = self.clock.tick()
            if actual_ts != validated_note.lamport_ts:
                validated_note = FieldNote(
                    note_id=validated_note.note_id,
                    site_code=validated_note.site_code,
                    title=validated_note.title,
                    body=validated_note.body,
                    severity=validated_note.severity,
                    location=validated_note.location,
                    author_device_id=validated_note.author_device_id,
                    version=validated_note.version,
                    lamport_ts=actual_ts,
                    sync_state=SyncState.PENDING_PUSH,
                    updated_at=validated_note.updated_at,
                    deleted=validated_note.deleted,
                )

            pending_note, mutation = self.device_store.upsert_local_note_offline(
                validated_note,
                base_version=base_version,
                operation=operation,
            )
            return {
                "note": pending_note.to_dict(),
                "mutation": _mutation_to_dict(mutation),
                "is_online": self.is_online,
            }

    def simulate_remote_edit(self, note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Simulate another technician modifying a note directly on the remote sync server."""
        if not isinstance(note_id, str) or not note_id.strip():
            raise InvalidFieldNoteError("note_id must be a non-empty string.")
        if not isinstance(payload, Mapping):
            raise InvalidFieldNoteError("Remote edit payload must be a JSON object.")

        with self._lock:
            existing_remote = self.remote_store.get_remote_note(note_id)
            title = payload["title"] if "title" in payload else existing_remote.title
            body = payload["body"] if "body" in payload else existing_remote.body
            severity: InspectionSeverity | None = None
            if "severity" in payload and payload["severity"] is not None:
                severity = _coerce_severity_input(payload["severity"])
            author_device_id = (
                payload["author_device_id"]
                if "author_device_id" in payload and payload["author_device_id"] is not None
                else "dispatch-web-02"
            )

            max_remote_ts = max(
                (n.lamport_ts for n in self.remote_store.list_remote_notes()),
                default=0,
            )
            updated = self.remote_store.force_remote_edit(
                note_id=note_id,
                title=title,
                body=body,
                severity=severity,
                author_device_id=author_device_id,
            )

            # Ensure server-wide causal monotonicity so delta pulls after _last_pull_lamport_ts observe this edit.
            watermark = max(max_remote_ts, self._last_pull_lamport_ts)
            if updated.lamport_ts <= watermark:
                updated = self.remote_store.force_save_resolved_note(
                    FieldNote(
                        note_id=updated.note_id,
                        site_code=updated.site_code,
                        title=updated.title,
                        body=updated.body,
                        severity=updated.severity,
                        location=updated.location,
                        author_device_id=updated.author_device_id,
                        version=updated.version,
                        lamport_ts=watermark + 1,
                        sync_state=SyncState.SYNCED,
                        updated_at=updated.updated_at,
                        deleted=updated.deleted,
                    )
                )

            note_dict = updated.to_dict()
            return {
                "note": note_dict,
                "remote_note": note_dict,
                "simulated_by": updated.author_device_id,
            }

    def run_delta_sync(self) -> dict[str, Any]:
        """Execute bidirectional outbox push + causal delta pull with optimistic conflict detection."""
        with self._lock:
            if not self.is_online:
                raise OfflineNetworkError(
                    "Device is offline; mutations remain safely queued in local SQLite outbox",
                    device_id=self.device_store.device_id,
                )

            pushed_count = 0
            pulled_count = 0
            conflicts_detected = 0
            pushed_note_ids: set[str] = set()

            # Phase 1: Drain queued outbox mutations in strict causal order.
            queued_mutations = self.device_store.list_pending_mutations(only_queued=True)
            for mutation in queued_mutations:
                try:
                    mutation_note = FieldNote.from_dict(json.loads(mutation.payload_json))
                except (ValueError, TypeError, InvalidFieldNoteError):
                    mutation_note = self.device_store.get_local_note(mutation.note_id)

                accepted, result_note = self.remote_store.push_mutation(
                    mutation_note,
                    base_version=mutation.base_version,
                )
                if accepted:
                    self.device_store.save_synced_note(result_note)
                    self.device_store.mark_mutation_status(
                        mutation.mutation_id,
                        MutationStatus.SYNCED,
                    )
                    self.clock.receive(result_note.lamport_ts)
                    pushed_note_ids.add(result_note.note_id)
                    pushed_count += 1
                else:
                    self.device_store.mark_mutation_status(
                        mutation.mutation_id,
                        MutationStatus.CONFLICT,
                    )
                    conflict = ConflictRecord(
                        conflict_id=f"conf-{uuid.uuid4().hex[:12]}",
                        note_id=mutation.note_id,
                        local_note=mutation_note,
                        remote_note=result_note,
                        detected_at=_utc_now_iso(),
                        resolved=False,
                        strategy=None,
                    )
                    self.device_store.record_conflict(conflict)
                    conflicts_detected += 1

            # Phase 2: Pull remote delta notes strictly newer than _last_pull_lamport_ts.
            delta_notes = self.remote_store.pull_delta_since(self._last_pull_lamport_ts)
            for remote_note in delta_notes:
                local_copy = self.device_store.get_note(remote_note.note_id)
                if local_copy is not None and local_copy.sync_state in (
                    SyncState.PENDING_PUSH,
                    SyncState.CONFLICT,
                ):
                    continue

                is_already_converged = remote_note.note_id in pushed_note_ids or (
                    local_copy is not None
                    and local_copy.sync_state is SyncState.SYNCED
                    and local_copy.version == remote_note.version
                    and local_copy.lamport_ts == remote_note.lamport_ts
                    and local_copy.title == remote_note.title
                    and local_copy.body == remote_note.body
                    and local_copy.severity == remote_note.severity
                    and local_copy.deleted == remote_note.deleted
                )
                if is_already_converged:
                    self._last_pull_lamport_ts = max(
                        self._last_pull_lamport_ts,
                        remote_note.lamport_ts,
                    )
                    continue

                self.device_store.save_synced_note(remote_note)
                self.clock.receive(remote_note.lamport_ts)
                pulled_count += 1
                self._last_pull_lamport_ts = max(
                    self._last_pull_lamport_ts,
                    remote_note.lamport_ts,
                )

            # Phase 3: Append immutable sync audit record.
            audit_status = "PARTIAL_CONFLICT" if conflicts_detected > 0 else "SUCCESS"
            audit_record = SyncAuditRecord(
                audit_id=f"aud-{uuid.uuid4().hex[:12]}",
                device_id=self.device_store.device_id,
                direction="BIDIRECTIONAL",
                pushed_count=pushed_count,
                pulled_count=pulled_count,
                conflicts_detected=conflicts_detected,
                status=audit_status,
                created_at=_utc_now_iso(),
            )
            persisted_audit = self.device_store.append_sync_audit(audit_record)
            self.remote_store.append_remote_sync_audit(persisted_audit)

            unresolved_conflicts = self.device_store.list_conflicts(unresolved_only=True)
            return {
                "pushed_count": pushed_count,
                "pulled_count": pulled_count,
                "conflicts_detected": conflicts_detected,
                "audit": _audit_to_dict(persisted_audit),
                "conflicts": [_conflict_to_dict(c) for c in unresolved_conflicts],
                "lamport_ts": self.clock.current(),
            }

    def resolve_conflict(self, conflict_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Deterministically resolve an active conflict and converge both local and remote stores."""
        if not isinstance(conflict_id, str) or not conflict_id.strip():
            raise InvalidConflictResolutionError("conflict_id must be a non-empty string.")
        if not isinstance(payload, Mapping):
            raise InvalidConflictResolutionError("Conflict resolution payload must be a JSON object.")

        with self._lock:
            if not self.is_online:
                raise OfflineNetworkError(
                    "Device is offline; cannot push conflict resolution to remote sync authority.",
                    device_id=self.device_store.device_id,
                )

            conflict = self.device_store.get_conflict(conflict_id)
            if conflict.resolved:
                existing_strategy = conflict.strategy.value if conflict.strategy is not None else "UNKNOWN"
                raise InvalidConflictResolutionError(
                    f"Conflict '{conflict_id}' is already resolved with strategy '{existing_strategy}'."
                )

            raw_strategy = payload.get("strategy")
            merged_title = payload.get("merged_title")
            merged_body = payload.get("merged_body")

            resolved_note = ConflictResolver.resolve(
                conflict.local_note,
                conflict.remote_note,
                strategy=raw_strategy,  # type: ignore[arg-type]
                merged_title=merged_title,
                merged_body=merged_body,
            )

            resolved_strategy = (
                raw_strategy
                if isinstance(raw_strategy, ConflictStrategy)
                else ConflictStrategy(str(raw_strategy))
            )

            synced_resolved = FieldNote(
                note_id=resolved_note.note_id,
                site_code=resolved_note.site_code,
                title=resolved_note.title,
                body=resolved_note.body,
                severity=resolved_note.severity,
                location=resolved_note.location,
                author_device_id=resolved_note.author_device_id,
                version=resolved_note.version,
                lamport_ts=resolved_note.lamport_ts,
                sync_state=SyncState.SYNCED,
                updated_at=resolved_note.updated_at,
                deleted=resolved_note.deleted,
            )

            saved_remote = self.remote_store.force_save_resolved_note(synced_resolved)
            updated_conflict = self.device_store.mark_conflict_resolved(
                conflict_id,
                resolved_strategy,
                saved_remote,
            )

            for mutation in self.device_store.list_pending_mutations(status=MutationStatus.CONFLICT):
                if mutation.note_id == conflict.note_id:
                    self.device_store.mark_mutation_status(
                        mutation.mutation_id,
                        MutationStatus.SYNCED,
                    )

            self.clock.receive(saved_remote.lamport_ts)
            self._last_pull_lamport_ts = max(
                self._last_pull_lamport_ts,
                saved_remote.lamport_ts,
            )

            audit_record = SyncAuditRecord(
                audit_id=f"aud-{uuid.uuid4().hex[:12]}",
                device_id=self.device_store.device_id,
                direction="CONFLICT_RESOLUTION",
                pushed_count=1,
                pulled_count=0,
                conflicts_detected=0,
                status=f"RESOLVED_{resolved_strategy.value}",
                created_at=_utc_now_iso(),
            )
            persisted_audit = self.device_store.append_sync_audit(audit_record)
            self.remote_store.append_remote_sync_audit(persisted_audit)

            return {
                "conflict": _conflict_to_dict(updated_conflict),
                "resolved_note": saved_remote.to_dict(),
                "audit": _audit_to_dict(persisted_audit),
            }

    def list_pending_mutations(self, only_queued: bool = False) -> list[dict[str, Any]]:
        """List local outbox mutations ordered causally."""
        with self._lock:
            return [
                _mutation_to_dict(m)
                for m in self.device_store.list_pending_mutations(only_queued=only_queued)
            ]

    def list_conflicts(self, unresolved_only: bool = False) -> list[dict[str, Any]]:
        """List local conflict records ordered newest-first."""
        with self._lock:
            return [
                _conflict_to_dict(c)
                for c in self.device_store.list_conflicts(unresolved_only=unresolved_only)
            ]

    def list_sync_audits(self, limit: int = 50) -> list[dict[str, Any]]:
        """List immutable sync audit records from the append-only ledger."""
        with self._lock:
            return [
                _audit_to_dict(a)
                for a in self.device_store.list_sync_audits(limit=limit)
            ]


__all__ = ["FieldNotesApplicationService"]
