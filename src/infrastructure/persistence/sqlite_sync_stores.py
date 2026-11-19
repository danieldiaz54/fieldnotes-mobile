"""SQLite local-first outbox, remote sync server store, and seed utilities."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
import uuid

from src.domain.exceptions import (
    InvalidConflictResolutionError,
    InvalidFieldNoteError,
    NoteNotFoundError,
    SyncConflictError,
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
    MutationOperation,
    MutationStatus,
    PendingMutation,
    SyncAuditRecord,
)


class AppendOnlyLedgerError(RuntimeError):
    """Raised when an UPDATE or DELETE is attempted on an append-only sync audit ledger."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_field_note(row: sqlite3.Row) -> FieldNote:
    return FieldNote(
        note_id=str(row["note_id"]),
        site_code=str(row["site_code"]),
        title=str(row["title"]),
        body=str(row["body"]),
        severity=InspectionSeverity(str(row["severity"])),
        location=GeoCoordinate(
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
        ),
        author_device_id=str(row["author_device_id"]),
        version=int(row["version"]),
        lamport_ts=int(row["lamport_ts"]),
        sync_state=SyncState(str(row["sync_state"])),
        updated_at=str(row["updated_at"]),
        deleted=bool(int(row["deleted"])),
    )


def _row_to_pending_mutation(row: sqlite3.Row) -> PendingMutation:
    return PendingMutation(
        mutation_id=str(row["mutation_id"]),
        note_id=str(row["note_id"]),
        device_id=str(row["device_id"]),
        operation=MutationOperation(str(row["operation"])),
        base_version=int(row["base_version"]),
        lamport_ts=int(row["lamport_ts"]),
        payload_json=str(row["payload_json"]),
        status=MutationStatus(str(row["status"])),
        created_at=str(row["created_at"]),
    )


def _row_to_conflict_record(row: sqlite3.Row) -> ConflictRecord:
    local_note = FieldNote.from_dict(json.loads(str(row["local_note_json"])))
    remote_note = FieldNote.from_dict(json.loads(str(row["remote_note_json"])))
    raw_strategy = row["strategy"]
    strategy = ConflictStrategy(str(raw_strategy)) if raw_strategy is not None else None
    return ConflictRecord(
        conflict_id=str(row["conflict_id"]),
        note_id=str(row["note_id"]),
        local_note=local_note,
        remote_note=remote_note,
        detected_at=str(row["detected_at"]),
        resolved=bool(int(row["resolved"])),
        strategy=strategy,
    )


def _row_to_sync_audit_record(row: sqlite3.Row) -> SyncAuditRecord:
    return SyncAuditRecord(
        audit_id=str(row["audit_id"]),
        device_id=str(row["device_id"]),
        direction=str(row["direction"]),
        pushed_count=int(row["pushed_count"]),
        pulled_count=int(row["pulled_count"]),
        conflicts_detected=int(row["conflicts_detected"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidFieldNoteError(
            f"{field_name} must be a non-bool integer >= 0; got {value!r}."
        )
    return value


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


class SQLiteDeviceStore:
    """On-device SQLite local-first store with ACID mutation outbox and append-only audit ledger.

    Conforms structurally to ``LocalDeviceStoreProtocol`` while providing high-level
    ACID offline-queuing and conflict-lifecycle operations.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        device_id: str = "device-ios-field-01",
    ) -> None:
        if not isinstance(device_id, str) or not device_id.strip():
            raise InvalidFieldNoteError("device_id must be a non-empty string.")
        self.db_path = db_path
        self.device_id = device_id
        self._lock = threading.RLock()
        if db_path != ":memory:":
            parent = Path(db_path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        ddl_script = """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS local_notes (
            note_id          TEXT PRIMARY KEY,
            site_code        TEXT NOT NULL,
            title            TEXT NOT NULL,
            body             TEXT NOT NULL,
            severity         TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
            latitude         REAL NOT NULL CHECK (latitude >= -90.0 AND latitude <= 90.0),
            longitude        REAL NOT NULL CHECK (longitude >= -180.0 AND longitude <= 180.0),
            author_device_id TEXT NOT NULL,
            version          INTEGER NOT NULL CHECK (version >= 1),
            lamport_ts       INTEGER NOT NULL CHECK (lamport_ts >= 1),
            sync_state       TEXT NOT NULL CHECK (sync_state IN ('SYNCED', 'PENDING_PUSH', 'CONFLICT')),
            updated_at       TEXT NOT NULL,
            deleted          INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
        );

        CREATE INDEX IF NOT EXISTS idx_local_notes_sync_state
            ON local_notes (sync_state, lamport_ts ASC);

        CREATE TABLE IF NOT EXISTS pending_mutations (
            mutation_id  TEXT PRIMARY KEY,
            note_id      TEXT NOT NULL,
            device_id    TEXT NOT NULL,
            operation    TEXT NOT NULL CHECK (operation IN ('UPSERT', 'DELETE')),
            base_version INTEGER NOT NULL CHECK (base_version >= 0),
            lamport_ts   INTEGER NOT NULL CHECK (lamport_ts >= 1),
            payload_json TEXT NOT NULL,
            status       TEXT NOT NULL CHECK (status IN ('QUEUED', 'SYNCED', 'CONFLICT')),
            created_at   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_pending_mutations_status_lamport
            ON pending_mutations (status, lamport_ts ASC, created_at ASC);

        CREATE TABLE IF NOT EXISTS local_conflicts (
            conflict_id      TEXT PRIMARY KEY,
            note_id          TEXT NOT NULL,
            local_note_json  TEXT NOT NULL,
            remote_note_json TEXT NOT NULL,
            detected_at      TEXT NOT NULL,
            resolved         INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
            strategy         TEXT CHECK (strategy IS NULL OR strategy IN ('KEEP_LOCAL', 'ACCEPT_REMOTE', 'MANUAL_MERGE')),
            resolved_at      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_local_conflicts_resolved
            ON local_conflicts (resolved, detected_at DESC);

        CREATE TABLE IF NOT EXISTS sync_audit_ledger (
            audit_id           TEXT PRIMARY KEY,
            device_id          TEXT NOT NULL,
            direction          TEXT NOT NULL,
            pushed_count       INTEGER NOT NULL CHECK (pushed_count >= 0),
            pulled_count       INTEGER NOT NULL CHECK (pulled_count >= 0),
            conflicts_detected INTEGER NOT NULL CHECK (conflicts_detected >= 0),
            status             TEXT NOT NULL,
            created_at         TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sync_audit_ledger_created_at
            ON sync_audit_ledger (created_at DESC);

        CREATE TRIGGER IF NOT EXISTS trg_sync_audit_ledger_no_update
        BEFORE UPDATE ON sync_audit_ledger
        BEGIN
            SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_sync_audit_ledger_no_delete
        BEFORE DELETE ON sync_audit_ledger
        BEGIN
            SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');
        END;
        """
        with self._lock:
            self._conn.executescript(ddl_script)
            self._conn.commit()

    def _upsert_local_note_row(self, note: FieldNote) -> None:
        self._conn.execute(
            """
            INSERT INTO local_notes (
                note_id, site_code, title, body, severity,
                latitude, longitude, author_device_id,
                version, lamport_ts, sync_state, updated_at, deleted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                site_code        = excluded.site_code,
                title            = excluded.title,
                body             = excluded.body,
                severity         = excluded.severity,
                latitude         = excluded.latitude,
                longitude        = excluded.longitude,
                author_device_id = excluded.author_device_id,
                version          = excluded.version,
                lamport_ts       = excluded.lamport_ts,
                sync_state       = excluded.sync_state,
                updated_at       = excluded.updated_at,
                deleted          = excluded.deleted
            """,
            (
                note.note_id,
                note.site_code,
                note.title,
                note.body,
                note.severity.value,
                note.location.latitude,
                note.location.longitude,
                note.author_device_id,
                note.version,
                note.lamport_ts,
                note.sync_state.value,
                note.updated_at,
                1 if note.deleted else 0,
            ),
        )

    def save_synced_note(self, note: FieldNote) -> FieldNote:
        """Persist an initial or pulled synced note without enqueueing an outbox mutation."""
        synced_note = FieldNote(
            note_id=note.note_id,
            site_code=note.site_code,
            title=note.title,
            body=note.body,
            severity=note.severity,
            location=note.location,
            author_device_id=note.author_device_id,
            version=note.version,
            lamport_ts=note.lamport_ts,
            sync_state=SyncState.SYNCED,
            updated_at=note.updated_at or _utc_now_iso(),
            deleted=note.deleted,
        )
        with self._lock:
            with self._conn:
                self._upsert_local_note_row(synced_note)
        return synced_note

    def upsert_local_note_offline(
        self,
        note: FieldNote,
        base_version: int,
        operation: MutationOperation = MutationOperation.UPSERT,
    ) -> tuple[FieldNote, PendingMutation]:
        """Atomically persist a local note (PENDING_PUSH) and enqueue a QUEUED outbox mutation."""
        validated_base_version = _require_non_negative_int(base_version, "base_version")
        resolved_op = (
            operation if isinstance(operation, MutationOperation) else MutationOperation(str(operation))
        )
        now_iso = _utc_now_iso()
        pending_note = FieldNote(
            note_id=note.note_id,
            site_code=note.site_code,
            title=note.title,
            body=note.body,
            severity=note.severity,
            location=note.location,
            author_device_id=note.author_device_id,
            version=note.version,
            lamport_ts=note.lamport_ts,
            sync_state=SyncState.PENDING_PUSH,
            updated_at=note.updated_at or now_iso,
            deleted=True if resolved_op is MutationOperation.DELETE else note.deleted,
        )
        mutation = PendingMutation(
            mutation_id=f"mut-{uuid.uuid4().hex[:12]}",
            note_id=pending_note.note_id,
            device_id=self.device_id,
            operation=resolved_op,
            base_version=validated_base_version,
            lamport_ts=pending_note.lamport_ts,
            payload_json=json.dumps(pending_note.to_dict(), sort_keys=True),
            status=MutationStatus.QUEUED,
            created_at=now_iso,
        )

        with self._lock:
            with self._conn:
                self._upsert_local_note_row(pending_note)
                self._conn.execute(
                    """
                    INSERT INTO pending_mutations (
                        mutation_id, note_id, device_id, operation,
                        base_version, lamport_ts, payload_json, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mutation.mutation_id,
                        mutation.note_id,
                        mutation.device_id,
                        mutation.operation.value,
                        mutation.base_version,
                        mutation.lamport_ts,
                        mutation.payload_json,
                        mutation.status.value,
                        mutation.created_at,
                    ),
                )

        return pending_note, mutation

    def list_local_notes(self) -> list[FieldNote]:
        """Return all locally persisted field notes ordered deterministically by note_id."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT note_id, site_code, title, body, severity,
                       latitude, longitude, author_device_id,
                       version, lamport_ts, sync_state, updated_at, deleted
                FROM local_notes
                ORDER BY note_id ASC
                """
            ).fetchall()
        return [_row_to_field_note(row) for row in rows]

    def get_local_note(self, note_id: str) -> FieldNote:
        """Return a local FieldNote by note_id or raise NoteNotFoundError."""
        note = self.get_note(note_id)
        if note is None:
            raise NoteNotFoundError(note_id)
        return note

    def list_pending_mutations(
        self,
        only_queued: bool = False,
        *,
        status: MutationStatus | None = None,
    ) -> list[PendingMutation]:
        """List outbox mutations in causal order (lamport_ts, created_at, mutation_id)."""
        target_status: str | None = None
        if status is not None:
            target_status = (
                status.value if isinstance(status, MutationStatus) else MutationStatus(str(status)).value
            )
        elif only_queued:
            target_status = MutationStatus.QUEUED.value

        with self._lock:
            if target_status is not None:
                rows = self._conn.execute(
                    """
                    SELECT mutation_id, note_id, device_id, operation,
                           base_version, lamport_ts, payload_json, status, created_at
                    FROM pending_mutations
                    WHERE status = ?
                    ORDER BY lamport_ts ASC, created_at ASC, mutation_id ASC
                    """,
                    (target_status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT mutation_id, note_id, device_id, operation,
                           base_version, lamport_ts, payload_json, status, created_at
                    FROM pending_mutations
                    ORDER BY lamport_ts ASC, created_at ASC, mutation_id ASC
                    """
                ).fetchall()
        return [_row_to_pending_mutation(row) for row in rows]

    def mark_mutation_status(self, mutation_id: str, status: MutationStatus) -> None:
        """Transition a persisted pending mutation to a new MutationStatus."""
        resolved_status = (
            status if isinstance(status, MutationStatus) else MutationStatus(str(status))
        )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE pending_mutations
                    SET status = ?
                    WHERE mutation_id = ?
                    """,
                    (resolved_status.value, mutation_id),
                )

    def record_conflict(self, conflict: ConflictRecord) -> ConflictRecord:
        """Persist a ConflictRecord and mark local_notes.sync_state = 'CONFLICT' atomically."""
        local_json = json.dumps(conflict.local_note.to_dict(), sort_keys=True)
        remote_json = json.dumps(conflict.remote_note.to_dict(), sort_keys=True)
        strategy_val = conflict.strategy.value if conflict.strategy is not None else None
        resolved_at = _utc_now_iso() if conflict.resolved else None

        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO local_conflicts (
                        conflict_id, note_id, local_note_json, remote_note_json,
                        detected_at, resolved, strategy, resolved_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conflict_id) DO UPDATE SET
                        note_id          = excluded.note_id,
                        local_note_json  = excluded.local_note_json,
                        remote_note_json = excluded.remote_note_json,
                        detected_at      = excluded.detected_at,
                        resolved         = excluded.resolved,
                        strategy         = excluded.strategy,
                        resolved_at      = excluded.resolved_at
                    """,
                    (
                        conflict.conflict_id,
                        conflict.note_id,
                        local_json,
                        remote_json,
                        conflict.detected_at,
                        1 if conflict.resolved else 0,
                        strategy_val,
                        resolved_at,
                    ),
                )
                if not conflict.resolved:
                    self._conn.execute(
                        """
                        UPDATE local_notes
                        SET sync_state = ?
                        WHERE note_id = ?
                        """,
                        (SyncState.CONFLICT.value, conflict.note_id),
                    )
        return conflict

    def list_conflicts(
        self,
        unresolved_only: bool = False,
        *,
        only_unresolved: bool | None = None,
    ) -> list[ConflictRecord]:
        """Return conflict records ordered newest-first, optionally filtered to unresolved only."""
        filter_unresolved = only_unresolved if only_unresolved is not None else unresolved_only
        with self._lock:
            if filter_unresolved:
                rows = self._conn.execute(
                    """
                    SELECT conflict_id, note_id, local_note_json, remote_note_json,
                           detected_at, resolved, strategy, resolved_at
                    FROM local_conflicts
                    WHERE resolved = 0
                    ORDER BY detected_at DESC, rowid DESC
                    """
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT conflict_id, note_id, local_note_json, remote_note_json,
                           detected_at, resolved, strategy, resolved_at
                    FROM local_conflicts
                    ORDER BY detected_at DESC, rowid DESC
                    """
                ).fetchall()
        return [_row_to_conflict_record(row) for row in rows]

    def get_conflict(self, conflict_id: str) -> ConflictRecord:
        """Retrieve a ConflictRecord by conflict_id or raise NoteNotFoundError if missing."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT conflict_id, note_id, local_note_json, remote_note_json,
                       detected_at, resolved, strategy, resolved_at
                FROM local_conflicts
                WHERE conflict_id = ?
                """,
                (conflict_id,),
            ).fetchone()
        if row is None:
            raise NoteNotFoundError(
                conflict_id,
                f"Conflict '{conflict_id}' was not found.",
            )
        return _row_to_conflict_record(row)

    def mark_conflict_resolved(
        self,
        conflict_id: str,
        strategy: ConflictStrategy,
        resolved_note: FieldNote,
    ) -> ConflictRecord:
        """Atomically resolve a conflict record and update local_notes with resolved_note."""
        existing = self.get_conflict(conflict_id)
        coerced_strategy = _coerce_conflict_strategy(strategy)
        if resolved_note.note_id != existing.note_id:
            raise InvalidConflictResolutionError(
                f"Resolved note_id '{resolved_note.note_id}' does not match "
                f"conflict note_id '{existing.note_id}'."
            )

        updated_conflict = ConflictRecord(
            conflict_id=existing.conflict_id,
            note_id=existing.note_id,
            local_note=existing.local_note,
            remote_note=existing.remote_note,
            detected_at=existing.detected_at,
            resolved=True,
            strategy=coerced_strategy,
        )
        now_iso = _utc_now_iso()
        persisted_note = FieldNote(
            note_id=resolved_note.note_id,
            site_code=resolved_note.site_code,
            title=resolved_note.title,
            body=resolved_note.body,
            severity=resolved_note.severity,
            location=resolved_note.location,
            author_device_id=resolved_note.author_device_id,
            version=resolved_note.version,
            lamport_ts=resolved_note.lamport_ts,
            sync_state=resolved_note.sync_state,
            updated_at=resolved_note.updated_at or now_iso,
            deleted=resolved_note.deleted,
        )

        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE local_conflicts
                    SET resolved = 1, strategy = ?, resolved_at = ?
                    WHERE conflict_id = ?
                    """,
                    (coerced_strategy.value, now_iso, conflict_id),
                )
                self._upsert_local_note_row(persisted_note)

        return updated_conflict

    def append_sync_audit(self, record: SyncAuditRecord) -> SyncAuditRecord:
        """Append an immutable SyncAuditRecord to sync_audit_ledger."""
        created_at = record.created_at or _utc_now_iso()
        persisted = SyncAuditRecord(
            audit_id=record.audit_id,
            device_id=record.device_id,
            direction=record.direction,
            pushed_count=record.pushed_count,
            pulled_count=record.pulled_count,
            conflicts_detected=record.conflicts_detected,
            status=record.status,
            created_at=created_at,
        )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO sync_audit_ledger (
                        audit_id, device_id, direction, pushed_count,
                        pulled_count, conflicts_detected, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        persisted.audit_id,
                        persisted.device_id,
                        persisted.direction,
                        persisted.pushed_count,
                        persisted.pulled_count,
                        persisted.conflicts_detected,
                        persisted.status,
                        persisted.created_at,
                    ),
                )
        return persisted

    def list_sync_audits(self, limit: int = 50) -> list[SyncAuditRecord]:
        """List immutable sync audit records ordered newest-first."""
        safe_limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT audit_id, device_id, direction, pushed_count,
                       pulled_count, conflicts_detected, status, created_at
                FROM sync_audit_ledger
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [_row_to_sync_audit_record(row) for row in rows]

    def execute_raw_mutation_for_test(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> None:
        """Execute a raw SQL statement and translate ledger trigger aborts to AppendOnlyLedgerError."""
        try:
            with self._lock:
                with self._conn:
                    self._conn.execute(sql, params)
        except (sqlite3.IntegrityError, sqlite3.DatabaseError) as exc:
            if "APPEND_ONLY_LEDGER_VIOLATION" in str(exc):
                raise AppendOnlyLedgerError(str(exc)) from exc
            raise

    # ------------------------------------------------------------------
    # LocalDeviceStoreProtocol structural conformance methods
    # ------------------------------------------------------------------

    def upsert_note(self, note: FieldNote) -> None:
        """Persist or replace a FieldNote in local device storage."""
        with self._lock:
            with self._conn:
                self._upsert_local_note_row(note)

    def get_note(self, note_id: str) -> FieldNote | None:
        """Retrieve a local FieldNote by identifier, or None if absent."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT note_id, site_code, title, body, severity,
                       latitude, longitude, author_device_id,
                       version, lamport_ts, sync_state, updated_at, deleted
                FROM local_notes
                WHERE note_id = ?
                """,
                (note_id,),
            ).fetchone()
        return _row_to_field_note(row) if row is not None else None

    def list_notes(self, *, include_deleted: bool = False) -> list[FieldNote]:
        """Return local field notes in deterministic order."""
        with self._lock:
            if include_deleted:
                rows = self._conn.execute(
                    """
                    SELECT note_id, site_code, title, body, severity,
                           latitude, longitude, author_device_id,
                           version, lamport_ts, sync_state, updated_at, deleted
                    FROM local_notes
                    ORDER BY note_id ASC
                    """
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT note_id, site_code, title, body, severity,
                           latitude, longitude, author_device_id,
                           version, lamport_ts, sync_state, updated_at, deleted
                    FROM local_notes
                    WHERE deleted = 0
                    ORDER BY note_id ASC
                    """
                ).fetchall()
        return [_row_to_field_note(row) for row in rows]

    def enqueue_mutation(self, mutation: PendingMutation) -> None:
        """Persist a PendingMutation into the durable local outbox queue."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO pending_mutations (
                        mutation_id, note_id, device_id, operation,
                        base_version, lamport_ts, payload_json, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mutation.mutation_id,
                        mutation.note_id,
                        mutation.device_id,
                        mutation.operation.value,
                        mutation.base_version,
                        mutation.lamport_ts,
                        mutation.payload_json,
                        mutation.status.value,
                        mutation.created_at,
                    ),
                )

    def update_mutation_status(self, mutation_id: str, status: MutationStatus) -> None:
        """Transition a persisted outbox mutation to a new MutationStatus."""
        self.mark_mutation_status(mutation_id, status)

    def save_conflict(self, conflict: ConflictRecord) -> None:
        """Insert or update a ConflictRecord in local storage."""
        self.record_conflict(conflict)

    def get_max_lamport_ts(self) -> int:
        """Return the highest persisted Lamport timestamp across local notes and outbox (0 if empty)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MAX(max_ts) AS highest_ts FROM (
                    SELECT COALESCE(MAX(lamport_ts), 0) AS max_ts FROM local_notes
                    UNION ALL
                    SELECT COALESCE(MAX(lamport_ts), 0) AS max_ts FROM pending_mutations
                )
                """
            ).fetchone()
        return int(row["highest_ts"]) if row is not None and row["highest_ts"] is not None else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()


class SQLiteRemoteSyncServerStore:
    """Authoritative remote sync server SQLite store with optimistic locking and append-only ledger.

    Conforms structurally to ``RemoteSyncStoreProtocol`` while providing conflict simulation
    and deterministic resolution persistence helpers.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        if db_path != ":memory:":
            parent = Path(db_path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        ddl_script = """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS remote_notes (
            note_id          TEXT PRIMARY KEY,
            site_code        TEXT NOT NULL,
            title            TEXT NOT NULL,
            body             TEXT NOT NULL,
            severity         TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
            latitude         REAL NOT NULL CHECK (latitude >= -90.0 AND latitude <= 90.0),
            longitude        REAL NOT NULL CHECK (longitude >= -180.0 AND longitude <= 180.0),
            author_device_id TEXT NOT NULL,
            version          INTEGER NOT NULL CHECK (version >= 1),
            lamport_ts       INTEGER NOT NULL CHECK (lamport_ts >= 1),
            sync_state       TEXT NOT NULL CHECK (sync_state IN ('SYNCED', 'PENDING_PUSH', 'CONFLICT')),
            updated_at       TEXT NOT NULL,
            deleted          INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
        );

        CREATE INDEX IF NOT EXISTS idx_remote_notes_lamport_ts
            ON remote_notes (lamport_ts ASC, note_id ASC);

        CREATE TABLE IF NOT EXISTS remote_sync_audit_ledger (
            audit_id           TEXT PRIMARY KEY,
            device_id          TEXT NOT NULL,
            direction          TEXT NOT NULL,
            pushed_count       INTEGER NOT NULL CHECK (pushed_count >= 0),
            pulled_count       INTEGER NOT NULL CHECK (pulled_count >= 0),
            conflicts_detected INTEGER NOT NULL CHECK (conflicts_detected >= 0),
            status             TEXT NOT NULL,
            created_at         TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_remote_sync_audit_ledger_created_at
            ON remote_sync_audit_ledger (created_at DESC);

        CREATE TRIGGER IF NOT EXISTS trg_remote_sync_audit_ledger_no_update
        BEFORE UPDATE ON remote_sync_audit_ledger
        BEGIN
            SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_remote_sync_audit_ledger_no_delete
        BEFORE DELETE ON remote_sync_audit_ledger
        BEGIN
            SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');
        END;
        """
        with self._lock:
            self._conn.executescript(ddl_script)
            self._conn.commit()

    def _upsert_remote_note_row(self, note: FieldNote) -> None:
        self._conn.execute(
            """
            INSERT INTO remote_notes (
                note_id, site_code, title, body, severity,
                latitude, longitude, author_device_id,
                version, lamport_ts, sync_state, updated_at, deleted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                site_code        = excluded.site_code,
                title            = excluded.title,
                body             = excluded.body,
                severity         = excluded.severity,
                latitude         = excluded.latitude,
                longitude        = excluded.longitude,
                author_device_id = excluded.author_device_id,
                version          = excluded.version,
                lamport_ts       = excluded.lamport_ts,
                sync_state       = excluded.sync_state,
                updated_at       = excluded.updated_at,
                deleted          = excluded.deleted
            """,
            (
                note.note_id,
                note.site_code,
                note.title,
                note.body,
                note.severity.value,
                note.location.latitude,
                note.location.longitude,
                note.author_device_id,
                note.version,
                note.lamport_ts,
                note.sync_state.value,
                note.updated_at,
                1 if note.deleted else 0,
            ),
        )

    def upsert_initial_note(self, note: FieldNote) -> FieldNote:
        """Persist an initial baseline note on the remote server as SYNCED."""
        saved = FieldNote(
            note_id=note.note_id,
            site_code=note.site_code,
            title=note.title,
            body=note.body,
            severity=note.severity,
            location=note.location,
            author_device_id=note.author_device_id,
            version=note.version,
            lamport_ts=note.lamport_ts,
            sync_state=SyncState.SYNCED,
            updated_at=note.updated_at or _utc_now_iso(),
            deleted=note.deleted,
        )
        with self._lock:
            with self._conn:
                self._upsert_remote_note_row(saved)
        return saved

    def list_remote_notes(self) -> list[FieldNote]:
        """Return all remote notes ordered deterministically by note_id."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT note_id, site_code, title, body, severity,
                       latitude, longitude, author_device_id,
                       version, lamport_ts, sync_state, updated_at, deleted
                FROM remote_notes
                ORDER BY note_id ASC
                """
            ).fetchall()
        return [_row_to_field_note(row) for row in rows]

    def get_remote_note(self, note_id: str) -> FieldNote:
        """Retrieve a remote FieldNote by note_id or raise NoteNotFoundError."""
        note = self.get_note(note_id)
        if note is None:
            raise NoteNotFoundError(note_id)
        return note

    def push_mutation(self, note: FieldNote, base_version: int) -> tuple[bool, FieldNote]:
        """Apply an optimistic-concurrency push against remote_notes without losing concurrent edits.

        - Absent remotely -> inserts at version=1, sync_state=SYNCED, returns (True, saved_note).
        - remote.version == base_version -> updates to version=base_version+1,
          lamport_ts=max(remote.lamport_ts, note.lamport_ts)+1, returns (True, saved_note).
        - remote.version != base_version -> rejects without overwriting, returns (False, remote).
        """
        validated_base_version = _require_non_negative_int(base_version, "base_version")
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    """
                    SELECT note_id, site_code, title, body, severity,
                           latitude, longitude, author_device_id,
                           version, lamport_ts, sync_state, updated_at, deleted
                    FROM remote_notes
                    WHERE note_id = ?
                    """,
                    (note.note_id,),
                ).fetchone()

                if row is None:
                    saved_note = FieldNote(
                        note_id=note.note_id,
                        site_code=note.site_code,
                        title=note.title,
                        body=note.body,
                        severity=note.severity,
                        location=note.location,
                        author_device_id=note.author_device_id,
                        version=1,
                        lamport_ts=note.lamport_ts,
                        sync_state=SyncState.SYNCED,
                        updated_at=_utc_now_iso(),
                        deleted=note.deleted,
                    )
                    self._upsert_remote_note_row(saved_note)
                    return True, saved_note

                remote = _row_to_field_note(row)
                if remote.version != validated_base_version:
                    return False, remote

                saved_note = FieldNote(
                    note_id=note.note_id,
                    site_code=note.site_code,
                    title=note.title,
                    body=note.body,
                    severity=note.severity,
                    location=note.location,
                    author_device_id=note.author_device_id,
                    version=validated_base_version + 1,
                    lamport_ts=max(remote.lamport_ts, note.lamport_ts) + 1,
                    sync_state=SyncState.SYNCED,
                    updated_at=_utc_now_iso(),
                    deleted=note.deleted,
                )
                self._upsert_remote_note_row(saved_note)
                return True, saved_note

    def force_save_resolved_note(self, resolved_note: FieldNote) -> FieldNote:
        """Persist a conflict-resolved note cleanly on the server preserving version and lamport_ts."""
        saved = FieldNote(
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
            updated_at=resolved_note.updated_at or _utc_now_iso(),
            deleted=resolved_note.deleted,
        )
        with self._lock:
            with self._conn:
                self._upsert_remote_note_row(saved)
        return saved

    def force_remote_edit(
        self,
        note_id: str,
        title: str,
        body: str,
        severity: InspectionSeverity | None = None,
        author_device_id: str = "dispatch-web-02",
    ) -> FieldNote:
        """Increment remote version + 1 and lamport_ts + 1 to simulate a concurrent edit."""
        with self._lock:
            with self._conn:
                row = self._conn.execute(
                    """
                    SELECT note_id, site_code, title, body, severity,
                           latitude, longitude, author_device_id,
                           version, lamport_ts, sync_state, updated_at, deleted
                    FROM remote_notes
                    WHERE note_id = ?
                    """,
                    (note_id,),
                ).fetchone()
                if row is None:
                    raise NoteNotFoundError(note_id)
                remote = _row_to_field_note(row)
                updated_note = FieldNote(
                    note_id=remote.note_id,
                    site_code=remote.site_code,
                    title=title,
                    body=body,
                    severity=severity if severity is not None else remote.severity,
                    location=remote.location,
                    author_device_id=author_device_id,
                    version=remote.version + 1,
                    lamport_ts=remote.lamport_ts + 1,
                    sync_state=SyncState.SYNCED,
                    updated_at=_utc_now_iso(),
                    deleted=remote.deleted,
                )
                self._upsert_remote_note_row(updated_note)
                return updated_note

    def pull_delta_since(self, since_lamport_ts: int) -> list[FieldNote]:
        """Return remote notes strictly newer than since_lamport_ts in causal order."""
        validated_since = _require_non_negative_int(since_lamport_ts, "since_lamport_ts")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT note_id, site_code, title, body, severity,
                       latitude, longitude, author_device_id,
                       version, lamport_ts, sync_state, updated_at, deleted
                FROM remote_notes
                WHERE lamport_ts > ?
                ORDER BY lamport_ts ASC, note_id ASC
                """,
                (validated_since,),
            ).fetchall()
        return [_row_to_field_note(row) for row in rows]

    def append_remote_sync_audit(self, record: SyncAuditRecord) -> SyncAuditRecord:
        """Append an immutable SyncAuditRecord to remote_sync_audit_ledger."""
        persisted = SyncAuditRecord(
            audit_id=record.audit_id,
            device_id=record.device_id,
            direction=record.direction,
            pushed_count=record.pushed_count,
            pulled_count=record.pulled_count,
            conflicts_detected=record.conflicts_detected,
            status=record.status,
            created_at=record.created_at or _utc_now_iso(),
        )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO remote_sync_audit_ledger (
                        audit_id, device_id, direction, pushed_count,
                        pulled_count, conflicts_detected, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        persisted.audit_id,
                        persisted.device_id,
                        persisted.direction,
                        persisted.pushed_count,
                        persisted.pulled_count,
                        persisted.conflicts_detected,
                        persisted.status,
                        persisted.created_at,
                    ),
                )
        return persisted

    def list_remote_sync_audits(self, limit: int = 50) -> list[SyncAuditRecord]:
        """List immutable remote sync audit records ordered newest-first."""
        safe_limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT audit_id, device_id, direction, pushed_count,
                       pulled_count, conflicts_detected, status, created_at
                FROM remote_sync_audit_ledger
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [_row_to_sync_audit_record(row) for row in rows]

    def execute_raw_mutation_for_test(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> None:
        """Execute raw SQL on the remote store, translating ledger aborts to AppendOnlyLedgerError."""
        try:
            with self._lock:
                with self._conn:
                    self._conn.execute(sql, params)
        except (sqlite3.IntegrityError, sqlite3.DatabaseError) as exc:
            if "APPEND_ONLY_LEDGER_VIOLATION" in str(exc):
                raise AppendOnlyLedgerError(str(exc)) from exc
            raise

    # ------------------------------------------------------------------
    # RemoteSyncStoreProtocol structural conformance methods
    # ------------------------------------------------------------------

    def get_note(self, note_id: str) -> FieldNote | None:
        """Retrieve the authoritative remote FieldNote by identifier, or None if absent."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT note_id, site_code, title, body, severity,
                       latitude, longitude, author_device_id,
                       version, lamport_ts, sync_state, updated_at, deleted
                FROM remote_notes
                WHERE note_id = ?
                """,
                (note_id,),
            ).fetchone()
        return _row_to_field_note(row) if row is not None else None

    def list_notes(
        self,
        *,
        since_lamport_ts: int = 0,
        include_deleted: bool = True,
    ) -> list[FieldNote]:
        """Pull remote notes with lamport_ts > since_lamport_ts in deterministic causal order."""
        validated_since = _require_non_negative_int(since_lamport_ts, "since_lamport_ts")
        with self._lock:
            if include_deleted:
                rows = self._conn.execute(
                    """
                    SELECT note_id, site_code, title, body, severity,
                           latitude, longitude, author_device_id,
                           version, lamport_ts, sync_state, updated_at, deleted
                    FROM remote_notes
                    WHERE lamport_ts > ?
                    ORDER BY lamport_ts ASC, note_id ASC
                    """,
                    (validated_since,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT note_id, site_code, title, body, severity,
                           latitude, longitude, author_device_id,
                           version, lamport_ts, sync_state, updated_at, deleted
                    FROM remote_notes
                    WHERE lamport_ts > ? AND deleted = 0
                    ORDER BY lamport_ts ASC, note_id ASC
                    """,
                    (validated_since,),
                ).fetchall()
        return [_row_to_field_note(row) for row in rows]

    def push_note(self, note: FieldNote, *, expected_base_version: int) -> FieldNote:
        """Atomically verify optimistic concurrency and persist a pushed FieldNote."""
        accepted, result_note = self.push_mutation(note, base_version=expected_base_version)
        if not accepted:
            raise SyncConflictError(
                note_id=note.note_id,
                local_version=expected_base_version,
                server_version=result_note.version,
            )
        return result_note

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()


__all__ = [
    "AppendOnlyLedgerError",
    "SQLiteDeviceStore",
    "SQLiteRemoteSyncServerStore",
]
