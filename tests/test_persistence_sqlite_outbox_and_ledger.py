"""SQLite device outbox, remote OCC sync store, seed, and append-only ledger tests."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

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
from src.domain.repositories import LocalDeviceStoreProtocol, RemoteSyncStoreProtocol
from src.domain.sync_engine import (
    ConflictRecord,
    MutationOperation,
    MutationStatus,
    SyncAuditRecord,
)
from src.infrastructure.persistence.seed_fieldnotes import DEFAULT_FIELD_NOTES, seed_synced_stores
from src.infrastructure.persistence.sqlite_sync_stores import (
    AppendOnlyLedgerError,
    SQLiteDeviceStore,
    SQLiteRemoteSyncServerStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SQL_PATH = REPO_ROOT / "migrations" / "001_init_fieldnotes_sync.sql"


class TestSQLiteProtocolsAndSeed(unittest.TestCase):
    """Verify structural protocol conformance, canonical seeding, and file durability."""

    def setUp(self) -> None:
        self.device_store = SQLiteDeviceStore(db_path=":memory:", device_id="device-ios-field-01")
        self.remote_store = SQLiteRemoteSyncServerStore(db_path=":memory:")

    def tearDown(self) -> None:
        self.device_store.close()
        self.remote_store.close()

    def test_stores_conform_to_domain_protocols(self) -> None:
        self.assertIsInstance(self.device_store, LocalDeviceStoreProtocol)
        self.assertIsInstance(self.remote_store, RemoteSyncStoreProtocol)

    def test_rejects_empty_device_id(self) -> None:
        with self.assertRaises(InvalidFieldNoteError):
            SQLiteDeviceStore(db_path=":memory:", device_id="   ")

    def test_seed_synced_stores_populates_five_canonical_notes(self) -> None:
        seeded_count = seed_synced_stores(self.device_store, self.remote_store)
        self.assertEqual(seeded_count, 5)
        self.assertEqual(len(DEFAULT_FIELD_NOTES), 5)

        local_notes = self.device_store.list_local_notes()
        remote_notes = self.remote_store.list_remote_notes()
        self.assertEqual(len(local_notes), 5)
        self.assertEqual(len(remote_notes), 5)
        self.assertEqual(local_notes, remote_notes)

        expected_ids = [f"NOTE-SITE-00{i}" for i in range(1, 6)]
        self.assertEqual([n.note_id for n in local_notes], expected_ids)
        self.assertEqual([n.version for n in local_notes], [1, 1, 1, 1, 1])
        self.assertEqual([n.lamport_ts for n in local_notes], [1, 2, 3, 4, 5])
        self.assertTrue(all(n.sync_state is SyncState.SYNCED for n in local_notes))
        self.assertEqual(self.device_store.get_max_lamport_ts(), 5)
        self.assertEqual(self.device_store.list_pending_mutations(), [])

    def test_file_backed_sqlite_persists_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dev_path = str(Path(tmpdir) / "device.sqlite3")
            rem_path = str(Path(tmpdir) / "remote.sqlite3")

            dev1 = SQLiteDeviceStore(db_path=dev_path, device_id="device-field-99")
            rem1 = SQLiteRemoteSyncServerStore(db_path=rem_path)
            try:
                seed_synced_stores(dev1, rem1)
            finally:
                dev1.close()
                rem1.close()

            dev2 = SQLiteDeviceStore(db_path=dev_path, device_id="device-field-99")
            rem2 = SQLiteRemoteSyncServerStore(db_path=rem_path)
            try:
                self.assertEqual(len(dev2.list_local_notes()), 5)
                self.assertEqual(len(rem2.list_remote_notes()), 5)
                self.assertEqual(dev2.get_max_lamport_ts(), 5)
            finally:
                dev2.close()
                rem2.close()


class TestAtomicLocalFirstOutbox(unittest.TestCase):
    """Verify ACID local-first writes, transactional rollback, causal ordering, and deletions."""

    def setUp(self) -> None:
        self.device_store = SQLiteDeviceStore(db_path=":memory:", device_id="device-ios-field-01")
        self.remote_store = SQLiteRemoteSyncServerStore(db_path=":memory:")
        seed_synced_stores(self.device_store, self.remote_store)

    def tearDown(self) -> None:
        self.device_store.close()
        self.remote_store.close()

    def test_atomic_offline_upsert_writes_local_note_and_queued_mutation(self) -> None:
        offline_note = FieldNote(
            note_id="NOTE-OFFLINE-101",
            site_code="SUB-ANDES-02",
            title="Offline Relay Panel Inspection",
            body="Captured without cellular coverage; stored in local outbox.",
            severity=InspectionSeverity.WARNING,
            location=GeoCoordinate(4.7200, -74.0800),
            author_device_id="device-ios-field-01",
            version=1,
            lamport_ts=6,
            sync_state=SyncState.SYNCED,  # Must be forced to PENDING_PUSH
            updated_at="2026-11-18T11:00:00+00:00",
        )

        saved_note, mutation = self.device_store.upsert_local_note_offline(
            offline_note,
            base_version=0,
            operation=MutationOperation.UPSERT,
        )

        self.assertIs(saved_note.sync_state, SyncState.PENDING_PUSH)
        self.assertEqual(mutation.note_id, "NOTE-OFFLINE-101")
        self.assertEqual(mutation.device_id, "device-ios-field-01")
        self.assertIs(mutation.operation, MutationOperation.UPSERT)
        self.assertEqual(mutation.base_version, 0)
        self.assertEqual(mutation.lamport_ts, 6)
        self.assertIs(mutation.status, MutationStatus.QUEUED)

        persisted_note = self.device_store.get_local_note("NOTE-OFFLINE-101")
        self.assertEqual(persisted_note, saved_note)
        self.assertEqual(FieldNote.from_dict(json.loads(mutation.payload_json)), saved_note)

        queued = self.device_store.list_pending_mutations(only_queued=True)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0], mutation)
        self.assertEqual(self.device_store.get_max_lamport_ts(), 6)

    def test_offline_write_rolls_back_atomically_if_outbox_insert_fails(self) -> None:
        original = self.device_store.get_local_note("NOTE-SITE-001")
        # Install a temporary trigger that forces pending_mutations insert to abort.
        self.device_store.execute_raw_mutation_for_test(
            """
            CREATE TRIGGER trg_test_fail_outbox_insert
            BEFORE INSERT ON pending_mutations
            BEGIN
                SELECT RAISE(ABORT, 'SIMULATED_OUTBOX_FAILURE');
            END;
            """
        )

        mutated = FieldNote(
            note_id=original.note_id,
            site_code=original.site_code,
            title="Should Roll Back Completely",
            body="Must never commit to local_notes if pending_mutations insert aborts.",
            severity=original.severity,
            location=original.location,
            author_device_id=original.author_device_id,
            version=2,
            lamport_ts=9,
            sync_state=SyncState.PENDING_PUSH,
            updated_at="2026-11-18T12:00:00+00:00",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.device_store.upsert_local_note_offline(mutated, base_version=1)

        # Verify rollback left local_notes and pending_mutations completely unchanged.
        after = self.device_store.get_local_note("NOTE-SITE-001")
        self.assertEqual(after, original)
        self.assertEqual(self.device_store.list_pending_mutations(), [])

    def test_causal_outbox_ordering_status_transitions_and_delete_filtering(self) -> None:
        note_a = FieldNote(
            note_id="NOTE-ORDER-A",
            site_code="SITE-A",
            title="Later Lamport Timestamp",
            body="Written first with lamport_ts=20",
            severity=InspectionSeverity.INFO,
            location=GeoCoordinate(1.0, 2.0),
            author_device_id="device-ios-field-01",
            version=1,
            lamport_ts=20,
            sync_state=SyncState.PENDING_PUSH,
            updated_at="2026-11-18T12:01:00+00:00",
        )
        note_b = FieldNote(
            note_id="NOTE-ORDER-B",
            site_code="SITE-B",
            title="Earlier Lamport Timestamp",
            body="Written second with lamport_ts=10",
            severity=InspectionSeverity.WARNING,
            location=GeoCoordinate(3.0, 4.0),
            author_device_id="device-ios-field-01",
            version=1,
            lamport_ts=10,
            sync_state=SyncState.PENDING_PUSH,
            updated_at="2026-11-18T12:02:00+00:00",
        )

        _, mut_a = self.device_store.upsert_local_note_offline(note_a, base_version=0)
        deleted_note, mut_b = self.device_store.upsert_local_note_offline(
            note_b,
            base_version=0,
            operation=MutationOperation.DELETE,
        )
        self.assertTrue(deleted_note.deleted)

        ordered = self.device_store.list_pending_mutations(only_queued=True)
        self.assertEqual([m.mutation_id for m in ordered], [mut_b.mutation_id, mut_a.mutation_id])

        self.device_store.mark_mutation_status(mut_b.mutation_id, MutationStatus.SYNCED)
        self.assertEqual(
            [m.mutation_id for m in self.device_store.list_pending_mutations(only_queued=True)],
            [mut_a.mutation_id],
        )
        self.assertEqual(
            [m.mutation_id for m in self.device_store.list_pending_mutations(status=MutationStatus.SYNCED)],
            [mut_b.mutation_id],
        )

        active_ids = [n.note_id for n in self.device_store.list_notes(include_deleted=False)]
        all_ids = [n.note_id for n in self.device_store.list_notes(include_deleted=True)]
        self.assertNotIn("NOTE-ORDER-B", active_ids)
        self.assertIn("NOTE-ORDER-B", all_ids)

        with self.assertRaises(NoteNotFoundError):
            self.device_store.get_local_note("NOTE-MISSING-XYZ")


class TestRemoteOCCAndConflictLifecycle(unittest.TestCase):
    """Verify optimistic concurrency control, pull_delta_since, and conflict persistence."""

    def setUp(self) -> None:
        self.device_store = SQLiteDeviceStore(db_path=":memory:", device_id="device-ios-field-01")
        self.remote_store = SQLiteRemoteSyncServerStore(db_path=":memory:")
        seed_synced_stores(self.device_store, self.remote_store)

    def tearDown(self) -> None:
        self.device_store.close()
        self.remote_store.close()

    def test_remote_occ_accepts_new_and_fast_forward_and_rejects_stale_without_clobbering(self) -> None:
        # 1. New note absent remotely -> accepted at version=1
        brand_new = FieldNote(
            note_id="NOTE-NEW-007",
            site_code="SUB-NORTH-07",
            title="New Substation Switchgear Check",
            body="SF6 pressure nominal at 6.2 bar.",
            severity=InspectionSeverity.INFO,
            location=GeoCoordinate(10.0, -75.0),
            author_device_id="device-ios-field-01",
            version=1,
            lamport_ts=6,
            sync_state=SyncState.PENDING_PUSH,
            updated_at="2026-11-18T13:00:00+00:00",
        )
        accepted, saved = self.remote_store.push_mutation(brand_new, base_version=0)
        self.assertTrue(accepted)
        self.assertEqual(saved.version, 1)
        self.assertIs(saved.sync_state, SyncState.SYNCED)

        # 2. Fast-forward update on NOTE-SITE-002 (remote.version == base_version == 1)
        note2 = self.remote_store.get_remote_note("NOTE-SITE-002")
        updated_note2 = FieldNote(
            note_id=note2.note_id,
            site_code=note2.site_code,
            title="Fast-Forward Valve Actuator Update",
            body="Pressure restored to 145 bar.",
            severity=InspectionSeverity.INFO,
            location=note2.location,
            author_device_id="device-ios-field-01",
            version=2,
            lamport_ts=10,
            sync_state=SyncState.PENDING_PUSH,
            updated_at="2026-11-18T13:05:00+00:00",
        )
        ff_ok, ff_saved = self.remote_store.push_mutation(updated_note2, base_version=1)
        self.assertTrue(ff_ok)
        self.assertEqual(ff_saved.version, 2)
        self.assertEqual(ff_saved.lamport_ts, 11)  # max(remote=2, note=10) + 1
        self.assertIs(ff_saved.sync_state, SyncState.SYNCED)

        # 3. Concurrent remote edit bumps NOTE-SITE-001 to version=2
        remote_edited = self.remote_store.force_remote_edit(
            note_id="NOTE-SITE-001",
            title="Dispatch Authoritative Update v2",
            body="Dispatch assigned Crew Bravo to Phase-B bushing.",
            severity=InspectionSeverity.CRITICAL,
            author_device_id="dispatch-web-02",
        )
        self.assertEqual(remote_edited.version, 2)
        self.assertEqual(remote_edited.lamport_ts, 2)

        # 4. Stale offline push (base_version=1) MUST be rejected without clobbering server data
        stale_local = FieldNote(
            note_id="NOTE-SITE-001",
            site_code="SUB-ANDES-01",
            title="Stale Offline Local Edit",
            body="Local technician edited offline from v1.",
            severity=InspectionSeverity.CRITICAL,
            location=remote_edited.location,
            author_device_id="device-ios-field-01",
            version=2,
            lamport_ts=12,
            sync_state=SyncState.PENDING_PUSH,
            updated_at="2026-11-18T13:10:00+00:00",
        )
        ok, authoritative = self.remote_store.push_mutation(stale_local, base_version=1)
        self.assertFalse(ok)
        self.assertEqual(authoritative, remote_edited)
        self.assertEqual(
            self.remote_store.get_remote_note("NOTE-SITE-001").title,
            "Dispatch Authoritative Update v2",
        )

        # Protocol push_note raises typed SyncConflictError on divergence without clobbering.
        with self.assertRaises(SyncConflictError) as ctx:
            self.remote_store.push_note(stale_local, expected_base_version=1)
        self.assertEqual(ctx.exception.local_version, 1)
        self.assertEqual(ctx.exception.server_version, 2)
        self.assertEqual(
            self.remote_store.get_remote_note("NOTE-SITE-001").title,
            "Dispatch Authoritative Update v2",
        )

    def test_pull_delta_since_returns_strictly_newer_notes_in_causal_order(self) -> None:
        self.assertEqual(
            [n.note_id for n in self.remote_store.pull_delta_since(3)],
            ["NOTE-SITE-004", "NOTE-SITE-005"],
        )
        self.assertEqual(self.remote_store.pull_delta_since(5), [])

        with self.assertRaises(InvalidFieldNoteError):
            self.remote_store.pull_delta_since(-1)
        with self.assertRaises(InvalidFieldNoteError):
            self.remote_store.pull_delta_since(True)  # type: ignore[arg-type]

        with self.assertRaises(NoteNotFoundError):
            self.remote_store.force_remote_edit("NOTE-MISSING", "T", "B")

    def test_conflict_persistence_and_atomic_resolution_in_device_store(self) -> None:
        local_note = self.device_store.get_local_note("NOTE-SITE-001")
        remote_note = self.remote_store.force_remote_edit(
            "NOTE-SITE-001",
            title="Remote v2 Title",
            body="Remote v2 Body",
        )
        conflict = ConflictRecord(
            conflict_id="conf-persist-001",
            note_id="NOTE-SITE-001",
            local_note=local_note,
            remote_note=remote_note,
            detected_at="2026-11-18T14:00:00+00:00",
            resolved=False,
            strategy=None,
        )
        self.device_store.record_conflict(conflict)

        # Local note must atomically transition to CONFLICT
        self.assertIs(self.device_store.get_local_note("NOTE-SITE-001").sync_state, SyncState.CONFLICT)
        self.assertEqual(len(self.device_store.list_conflicts(unresolved_only=True)), 1)

        # Mismatched note_id during resolution fails cleanly
        wrong_note = self.device_store.get_local_note("NOTE-SITE-002")
        with self.assertRaises(InvalidConflictResolutionError):
            self.device_store.mark_conflict_resolved(
                "conf-persist-001",
                ConflictStrategy.KEEP_LOCAL,
                wrong_note,
            )

        # Missing conflict raises NoteNotFoundError
        with self.assertRaises(NoteNotFoundError):
            self.device_store.get_conflict("conf-does-not-exist")

        resolved_note = FieldNote(
            note_id="NOTE-SITE-001",
            site_code=local_note.site_code,
            title="Converged v3",
            body="Converged body",
            severity=local_note.severity,
            location=local_note.location,
            author_device_id=local_note.author_device_id,
            version=3,
            lamport_ts=9,
            sync_state=SyncState.SYNCED,
            updated_at="2026-11-18T14:05:00+00:00",
        )
        updated_conflict = self.device_store.mark_conflict_resolved(
            "conf-persist-001",
            ConflictStrategy.MANUAL_MERGE,
            resolved_note,
        )
        self.assertTrue(updated_conflict.resolved)
        self.assertIs(updated_conflict.strategy, ConflictStrategy.MANUAL_MERGE)
        self.assertEqual(self.device_store.list_conflicts(unresolved_only=True), [])
        self.assertEqual(self.device_store.get_local_note("NOTE-SITE-001").version, 3)
        self.assertIs(self.device_store.get_local_note("NOTE-SITE-001").sync_state, SyncState.SYNCED)


class TestAppendOnlyAuditLedgerTriggers(unittest.TestCase):
    """Verify database-level BEFORE UPDATE and BEFORE DELETE append-only immutability triggers."""

    def setUp(self) -> None:
        self.device_store = SQLiteDeviceStore(db_path=":memory:", device_id="device-ios-field-01")
        self.remote_store = SQLiteRemoteSyncServerStore(db_path=":memory:")

    def tearDown(self) -> None:
        self.device_store.close()
        self.remote_store.close()

    def test_local_sync_audit_ledger_blocks_update_and_delete(self) -> None:
        record = SyncAuditRecord(
            audit_id="aud-local-001",
            device_id="device-ios-field-01",
            direction="BIDIRECTIONAL",
            pushed_count=2,
            pulled_count=1,
            conflicts_detected=0,
            status="SUCCESS",
            created_at="2026-11-18T15:00:00+00:00",
        )
        self.device_store.append_sync_audit(record)
        self.assertEqual(self.device_store.list_sync_audits(), [record])

        with self.assertRaises(AppendOnlyLedgerError) as upd_ctx:
            self.device_store.execute_raw_mutation_for_test(
                "UPDATE sync_audit_ledger SET status = ? WHERE audit_id = ?",
                ("TAMPERED", "aud-local-001"),
            )
        self.assertIn("APPEND_ONLY_LEDGER_VIOLATION", str(upd_ctx.exception))

        with self.assertRaises(AppendOnlyLedgerError) as del_ctx:
            self.device_store.execute_raw_mutation_for_test(
                "DELETE FROM sync_audit_ledger WHERE audit_id = ?",
                ("aud-local-001",),
            )
        self.assertIn("APPEND_ONLY_LEDGER_VIOLATION", str(del_ctx.exception))

        # Ledger entry must remain completely intact.
        self.assertEqual(self.device_store.list_sync_audits(), [record])

    def test_remote_sync_audit_ledger_blocks_update_and_delete(self) -> None:
        record = SyncAuditRecord(
            audit_id="aud-remote-001",
            device_id="device-ios-field-01",
            direction="BIDIRECTIONAL",
            pushed_count=1,
            pulled_count=0,
            conflicts_detected=1,
            status="PARTIAL_CONFLICT",
            created_at="2026-11-18T15:05:00+00:00",
        )
        self.remote_store.append_remote_sync_audit(record)
        self.assertEqual(self.remote_store.list_remote_sync_audits(), [record])

        with self.assertRaises(AppendOnlyLedgerError) as upd_ctx:
            self.remote_store.execute_raw_mutation_for_test(
                "UPDATE remote_sync_audit_ledger SET pushed_count = ? WHERE audit_id = ?",
                (999, "aud-remote-001"),
            )
        self.assertIn("APPEND_ONLY_LEDGER_VIOLATION", str(upd_ctx.exception))

        with self.assertRaises(AppendOnlyLedgerError) as del_ctx:
            self.remote_store.execute_raw_mutation_for_test(
                "DELETE FROM remote_sync_audit_ledger WHERE audit_id = ?",
                ("aud-remote-001",),
            )
        self.assertIn("APPEND_ONLY_LEDGER_VIOLATION", str(del_ctx.exception))

        # Remote ledger entry must remain completely intact.
        self.assertEqual(self.remote_store.list_remote_sync_audits(), [record])

    def test_postgres_migration_defines_append_only_triggers(self) -> None:
        self.assertTrue(MIGRATION_SQL_PATH.exists())
        sql_text = MIGRATION_SQL_PATH.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS remote_notes", sql_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS sync_audit_ledger", sql_text)
        self.assertIn("prevent_sync_audit_mutation", sql_text)
        self.assertIn("BEFORE UPDATE ON sync_audit_ledger", sql_text)
        self.assertIn("BEFORE DELETE ON sync_audit_ledger", sql_text)
        self.assertIn("APPEND_ONLY_LEDGER_VIOLATION", sql_text)


if __name__ == "__main__":
    unittest.main()
