"""Domain invariant, Lamport clock, conflict resolver, purity, and mobile manifest tests."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
import re
import threading
import unittest

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
from src.domain.sync_engine import (
    ConflictRecord,
    ConflictResolver,
    LamportClock,
    MutationOperation,
    MutationStatus,
    PendingMutation,
    SyncAuditRecord,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_DIR = REPO_ROOT / "src" / "domain"
MOBILE_APP_DIR = REPO_ROOT / "mobile-app"


def _make_note(
    *,
    note_id: str = "NOTE-SITE-001",
    site_code: str = "SUB-ANDES-01",
    title: str = "Transformer Bushing Hotspot",
    body: str = "Thermal camera measured 94.2 C on Phase-B bushing.",
    severity: InspectionSeverity = InspectionSeverity.CRITICAL,
    location: GeoCoordinate | None = None,
    author_device_id: str = "device-ios-field-01",
    version: int = 1,
    lamport_ts: int = 1,
    sync_state: SyncState = SyncState.SYNCED,
    updated_at: str = "2026-11-18T08:00:00+00:00",
    deleted: bool = False,
) -> FieldNote:
    return FieldNote(
        note_id=note_id,
        site_code=site_code,
        title=title,
        body=body,
        severity=severity,
        location=location if location is not None else GeoCoordinate(4.7110, -74.0721),
        author_device_id=author_device_id,
        version=version,
        lamport_ts=lamport_ts,
        sync_state=sync_state,
        updated_at=updated_at,
        deleted=deleted,
    )


class TestDomainExceptions(unittest.TestCase):
    """Verify explicit machine-readable codes and metadata on typed domain exceptions."""

    def test_exception_codes_and_attributes(self) -> None:
        base = DomainError("Generic failure", details={"key": "val"})
        self.assertEqual(base.code, "ERR-500-DOMAIN-ERROR")
        self.assertEqual(base.details, {"key": "val"})

        invalid = InvalidFieldNoteError("Bad coordinate")
        self.assertEqual(invalid.code, "ERR-400-INVALID-FIELD-NOTE")
        self.assertIsInstance(invalid, DomainError)

        missing = NoteNotFoundError("NOTE-404")
        self.assertEqual(missing.code, "ERR-404-NOTE-NOT-FOUND")
        self.assertEqual(missing.note_id, "NOTE-404")
        self.assertEqual(missing.details, {"note_id": "NOTE-404"})

        conflict = SyncConflictError("NOTE-1", local_version=1, server_version=3)
        self.assertEqual(conflict.code, "ERR-409-SYNC-CONFLICT")
        self.assertEqual(conflict.note_id, "NOTE-1")
        self.assertEqual(conflict.local_version, 1)
        self.assertEqual(conflict.server_version, 3)
        self.assertEqual(
            conflict.details,
            {"note_id": "NOTE-1", "local_version": 1, "server_version": 3},
        )

        bad_res = InvalidConflictResolutionError("Missing merged_body")
        self.assertEqual(bad_res.code, "ERR-422-INVALID-RESOLUTION")

        offline = OfflineNetworkError(device_id="device-ios-01")
        self.assertEqual(offline.code, "ERR-503-DEVICE-OFFLINE")
        self.assertEqual(offline.device_id, "device-ios-01")
        self.assertEqual(offline.details, {"device_id": "device-ios-01"})


class TestGeoCoordinateInvariants(unittest.TestCase):
    """Verify WGS84 bounds, finiteness, non-bool strictness, and immutability."""

    def test_valid_boundary_coordinates_and_int_normalization(self) -> None:
        cases = [
            (-90.0, -180.0),
            (90.0, 180.0),
            (0, 0),
            (-90, 180),
            (4.711, -74.0721),
        ]
        for lat, lon in cases:
            coord = GeoCoordinate(latitude=lat, longitude=lon)
            self.assertIsInstance(coord.latitude, float)
            self.assertIsInstance(coord.longitude, float)
            self.assertEqual(coord.latitude, float(lat))
            self.assertEqual(coord.longitude, float(lon))

    def test_geocoordinate_is_frozen_and_round_trips(self) -> None:
        coord = GeoCoordinate(6.2442, -75.5812)
        with self.assertRaises(FrozenInstanceError):
            coord.latitude = 0.0  # type: ignore[misc]

        payload = coord.to_dict()
        self.assertEqual(payload, {"latitude": 6.2442, "longitude": -75.5812})
        self.assertEqual(GeoCoordinate.from_dict(payload), coord)

    def test_rejects_out_of_range_coordinates(self) -> None:
        invalid_pairs = [
            (-90.0001, 0.0),
            (90.0001, 0.0),
            (-120.0, 10.0),
            (120.0, 10.0),
            (0.0, -180.0001),
            (0.0, 180.0001),
            (0.0, -250.0),
            (0.0, 250.0),
        ]
        for lat, lon in invalid_pairs:
            with self.subTest(lat=lat, lon=lon):
                with self.assertRaises(InvalidFieldNoteError):
                    GeoCoordinate(latitude=lat, longitude=lon)

    def test_rejects_nan_infinity_bool_and_non_numeric(self) -> None:
        bad_values: list[object] = [
            math.nan,
            float("nan"),
            math.inf,
            -math.inf,
            True,
            False,
            "4.711",
            None,
        ]
        for bad in bad_values:
            with self.subTest(field="latitude", value=bad):
                with self.assertRaises(InvalidFieldNoteError):
                    GeoCoordinate(latitude=bad, longitude=0.0)  # type: ignore[arg-type]
            with self.subTest(field="longitude", value=bad):
                with self.assertRaises(InvalidFieldNoteError):
                    GeoCoordinate(latitude=0.0, longitude=bad)  # type: ignore[arg-type]

    def test_from_dict_rejects_invalid_payloads(self) -> None:
        with self.assertRaises(InvalidFieldNoteError):
            GeoCoordinate.from_dict("not-a-mapping")  # type: ignore[arg-type]
        with self.assertRaises(InvalidFieldNoteError):
            GeoCoordinate.from_dict({"latitude": 4.711})
        with self.assertRaises(InvalidFieldNoteError):
            GeoCoordinate.from_dict({"longitude": -74.0721})


class TestFieldNoteInvariants(unittest.TestCase):
    """Verify FieldNote immutability, self-validation, and serialization contracts."""

    def test_valid_field_note_immutability_and_round_trip(self) -> None:
        note = _make_note(
            severity="WARNING",  # type: ignore[arg-type]
            sync_state="PENDING_PUSH",  # type: ignore[arg-type]
            version=2,
            lamport_ts=7,
        )
        self.assertIs(note.severity, InspectionSeverity.WARNING)
        self.assertIs(note.sync_state, SyncState.PENDING_PUSH)

        with self.assertRaises(FrozenInstanceError):
            note.title = "Mutated"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            note.version = 99  # type: ignore[misc]

        reconstructed = FieldNote.from_dict(note.to_dict())
        self.assertEqual(reconstructed, note)

    def test_rejects_empty_or_whitespace_required_strings(self) -> None:
        for field in ("note_id", "site_code", "title", "body", "author_device_id"):
            for bad_value in ("", "   ", "\t\n", None, 123):
                with self.subTest(field=field, value=bad_value):
                    kwargs = {field: bad_value}
                    with self.assertRaises(InvalidFieldNoteError):
                        _make_note(**kwargs)  # type: ignore[arg-type]

    def test_rejects_invalid_enums_location_counters_and_types(self) -> None:
        with self.assertRaises(InvalidFieldNoteError):
            _make_note(severity="EXTREME")  # type: ignore[arg-type]
        with self.assertRaises(InvalidFieldNoteError):
            _make_note(sync_state="DIRTY")  # type: ignore[arg-type]
        with self.assertRaises(InvalidFieldNoteError):
            _make_note(location={"latitude": 4.7, "longitude": -74.0})  # type: ignore[arg-type]

        for bad_int in (0, -1, True, False, 1.5, "1"):
            with self.subTest(field="version", value=bad_int):
                with self.assertRaises(InvalidFieldNoteError):
                    _make_note(version=bad_int)  # type: ignore[arg-type]
            with self.subTest(field="lamport_ts", value=bad_int):
                with self.assertRaises(InvalidFieldNoteError):
                    _make_note(lamport_ts=bad_int)  # type: ignore[arg-type]

        with self.assertRaises(InvalidFieldNoteError):
            _make_note(updated_at=12345)  # type: ignore[arg-type]
        for bad_deleted in (0, 1, "false", None):
            with self.subTest(deleted=bad_deleted):
                with self.assertRaises(InvalidFieldNoteError):
                    _make_note(deleted=bad_deleted)  # type: ignore[arg-type]

    def test_from_dict_rejects_missing_required_fields_or_non_mapping(self) -> None:
        with self.assertRaises(InvalidFieldNoteError):
            FieldNote.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]
        with self.assertRaises(InvalidFieldNoteError):
            FieldNote.from_dict({"note_id": "NOTE-1"})


class TestOutboxConflictAndAuditEntities(unittest.TestCase):
    """Verify frozen invariants for PendingMutation, ConflictRecord, and SyncAuditRecord."""

    def test_pending_mutation_validation(self) -> None:
        mut = PendingMutation(
            mutation_id="mut-001",
            note_id="NOTE-SITE-001",
            device_id="device-ios-field-01",
            operation="UPSERT",  # type: ignore[arg-type]
            base_version=0,
            lamport_ts=1,
            payload_json="{}",
            status="QUEUED",  # type: ignore[arg-type]
            created_at="2026-11-18T10:00:00+00:00",
        )
        self.assertIs(mut.operation, MutationOperation.UPSERT)
        self.assertIs(mut.status, MutationStatus.QUEUED)
        with self.assertRaises(FrozenInstanceError):
            mut.status = MutationStatus.SYNCED  # type: ignore[misc]

        with self.assertRaises(InvalidFieldNoteError):
            PendingMutation(
                mutation_id="mut-002",
                note_id="NOTE-SITE-001",
                device_id="device-ios-field-01",
                operation=MutationOperation.UPSERT,
                base_version=-1,
                lamport_ts=1,
                payload_json="{}",
                status=MutationStatus.QUEUED,
                created_at="",
            )
        with self.assertRaises(InvalidFieldNoteError):
            PendingMutation(
                mutation_id="mut-003",
                note_id="NOTE-SITE-001",
                device_id="device-ios-field-01",
                operation=MutationOperation.UPSERT,
                base_version=True,  # type: ignore[arg-type]
                lamport_ts=1,
                payload_json="{}",
                status=MutationStatus.QUEUED,
                created_at="",
            )

    def test_conflict_record_validation(self) -> None:
        local_note = _make_note(note_id="NOTE-1", version=2, lamport_ts=6)
        remote_note = _make_note(note_id="NOTE-1", version=2, lamport_ts=7)
        other_note = _make_note(note_id="NOTE-OTHER", version=1, lamport_ts=2)

        record = ConflictRecord(
            conflict_id="conf-1",
            note_id="NOTE-1",
            local_note=local_note,
            remote_note=remote_note,
            detected_at="2026-11-18T10:00:00+00:00",
            resolved=False,
            strategy=None,
        )
        self.assertFalse(record.resolved)

        with self.assertRaises(InvalidConflictResolutionError):
            ConflictRecord(
                conflict_id="conf-2",
                note_id="NOTE-1",
                local_note=local_note,
                remote_note=other_note,
                detected_at="2026-11-18T10:00:00+00:00",
            )
        with self.assertRaises(InvalidConflictResolutionError):
            ConflictRecord(
                conflict_id="conf-3",
                note_id="NOTE-1",
                local_note=local_note,
                remote_note=remote_note,
                detected_at="2026-11-18T10:00:00+00:00",
                resolved=True,
                strategy=None,
            )

    def test_sync_audit_record_validation(self) -> None:
        audit = SyncAuditRecord(
            audit_id="aud-1",
            device_id="device-ios-field-01",
            direction="BIDIRECTIONAL",
            pushed_count=0,
            pulled_count=2,
            conflicts_detected=1,
            status="PARTIAL_CONFLICT",
            created_at="2026-11-18T10:00:00+00:00",
        )
        self.assertEqual(audit.conflicts_detected, 1)
        with self.assertRaises(InvalidFieldNoteError):
            SyncAuditRecord(
                audit_id="aud-2",
                device_id="device-ios-field-01",
                direction="BIDIRECTIONAL",
                pushed_count=-1,
                pulled_count=0,
                conflicts_detected=0,
                status="SUCCESS",
                created_at="",
            )


class TestLamportClock(unittest.TestCase):
    """Verify monotonic tick(), causal receive(), validation, and thread safety."""

    def test_monotonic_tick_and_causal_receive_formula(self) -> None:
        clock = LamportClock()
        self.assertEqual(clock.current(), 0)
        self.assertEqual(clock.tick(), 1)
        self.assertEqual(clock.tick(), 2)
        self.assertEqual(clock.current(), 2)

        # Remote ahead of local: max(2, 10) + 1 == 11
        self.assertEqual(clock.receive(10), 11)
        # Remote equal to local: max(11, 11) + 1 == 12
        self.assertEqual(clock.receive(11), 12)
        # Remote behind local: max(12, 3) + 1 == 13
        self.assertEqual(clock.receive(3), 13)
        # Remote == 0 still strictly advances: max(13, 0) + 1 == 14
        self.assertEqual(clock.receive(0), 14)
        self.assertEqual(clock.current(), 14)

    def test_rejects_invalid_initial_and_remote_timestamps(self) -> None:
        for bad in (-1, -100, True, False, 2.5, "5", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidFieldNoteError):
                    LamportClock(initial=bad)  # type: ignore[arg-type]

        clock = LamportClock(initial=5)
        for bad in (-1, True, False, 1.2, "10", None):
            with self.subTest(remote=bad):
                with self.assertRaises(InvalidFieldNoteError):
                    clock.receive(bad)  # type: ignore[arg-type]
        self.assertEqual(clock.current(), 5)

    def test_thread_safe_concurrent_ticks_and_receives(self) -> None:
        clock = LamportClock(initial=0)
        num_threads = 12
        ticks_per_thread = 200
        collected: list[list[int]] = [[] for _ in range(num_threads)]
        barrier = threading.Barrier(num_threads)

        def worker(idx: int) -> None:
            barrier.wait()
            local_values = collected[idx]
            for _ in range(ticks_per_thread):
                local_values.append(clock.tick())

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        all_values = [ts for bucket in collected for ts in bucket]
        expected_total = num_threads * ticks_per_thread
        self.assertEqual(len(all_values), expected_total)
        self.assertEqual(len(set(all_values)), expected_total)
        self.assertEqual(sorted(all_values), list(range(1, expected_total + 1)))
        self.assertEqual(clock.current(), expected_total)

        # Concurrent mixed receive() + tick() preserves unique timestamps and causal lower bound.
        mixed_results: list[int] = []
        mixed_lock = threading.Lock()
        jump_target = expected_total + 500

        def mixed_worker(idx: int) -> None:
            local_out: list[int] = []
            for step in range(50):
                if (idx + step) % 5 == 0:
                    local_out.append(clock.receive(jump_target + step))
                else:
                    local_out.append(clock.tick())
            with mixed_lock:
                mixed_results.extend(local_out)

        mixed_threads = [threading.Thread(target=mixed_worker, args=(i,)) for i in range(8)]
        for thread in mixed_threads:
            thread.start()
        for thread in mixed_threads:
            thread.join()

        self.assertEqual(len(mixed_results), len(set(mixed_results)))
        self.assertGreater(clock.current(), jump_target + 49)


class TestConflictResolver(unittest.TestCase):
    """Verify deterministic OCC detection and all 3 conflict resolution strategies."""

    def setUp(self) -> None:
        self.local_note = _make_note(
            note_id="NOTE-SITE-001",
            site_code="SUB-ANDES-01",
            title="Local Field Reading: 98.4 C",
            body="Offline field FLIR reading reached 98.4 C; breaker tagged.",
            severity=InspectionSeverity.CRITICAL,
            location=GeoCoordinate(4.7110, -74.0721),
            author_device_id="device-ios-field-01",
            version=2,
            lamport_ts=8,
            sync_state=SyncState.CONFLICT,
            deleted=False,
        )
        self.remote_note = _make_note(
            note_id="NOTE-SITE-001",
            site_code="SUB-ANDES-01",
            title="Dispatch Update: Crew Bravo En Route",
            body="Replacement 230 kV porcelain bushing loaded on truck.",
            severity=InspectionSeverity.WARNING,
            location=GeoCoordinate(4.7115, -74.0725),
            author_device_id="dispatch-web-02",
            version=3,
            lamport_ts=6,
            sync_state=SyncState.SYNCED,
            deleted=False,
        )

    def test_version_conflict_detection_helpers(self) -> None:
        self.assertFalse(ConflictResolver.has_version_conflict(0, 0))
        self.assertFalse(ConflictResolver.has_version_conflict(2, 2))
        self.assertTrue(ConflictResolver.has_version_conflict(1, 2))

        ConflictResolver.assert_no_version_conflict("NOTE-SITE-001", 2, 2)
        with self.assertRaises(SyncConflictError) as ctx:
            ConflictResolver.assert_no_version_conflict("NOTE-SITE-001", 1, 3)
        self.assertEqual(ctx.exception.code, "ERR-409-SYNC-CONFLICT")
        self.assertEqual(ctx.exception.local_version, 1)
        self.assertEqual(ctx.exception.server_version, 3)

        with self.assertRaises(InvalidFieldNoteError):
            ConflictResolver.has_version_conflict(-1, 0)
        with self.assertRaises(InvalidFieldNoteError):
            ConflictResolver.has_version_conflict(True, 1)  # type: ignore[arg-type]

    def test_resolve_keep_local(self) -> None:
        resolved = ConflictResolver.resolve(
            self.local_note,
            self.remote_note,
            ConflictStrategy.KEEP_LOCAL,
            updated_at="2026-11-19T01:00:00+00:00",
        )
        self.assertEqual(resolved.note_id, "NOTE-SITE-001")
        self.assertEqual(resolved.title, self.local_note.title)
        self.assertEqual(resolved.body, self.local_note.body)
        self.assertEqual(resolved.severity, InspectionSeverity.CRITICAL)
        self.assertEqual(resolved.location, self.local_note.location)
        self.assertEqual(resolved.author_device_id, "device-ios-field-01")
        self.assertEqual(resolved.version, 4)  # max(2, 3) + 1
        self.assertEqual(resolved.lamport_ts, 9)  # max(8, 6) + 1
        self.assertIs(resolved.sync_state, SyncState.PENDING_PUSH)
        self.assertEqual(resolved.updated_at, "2026-11-19T01:00:00+00:00")

    def test_resolve_accept_remote(self) -> None:
        resolved = ConflictResolver.resolve(
            self.local_note,
            self.remote_note,
            "ACCEPT_REMOTE",
            updated_at="2026-11-19T01:05:00+00:00",
        )
        self.assertEqual(resolved.note_id, "NOTE-SITE-001")
        self.assertEqual(resolved.title, self.remote_note.title)
        self.assertEqual(resolved.body, self.remote_note.body)
        self.assertEqual(resolved.severity, InspectionSeverity.WARNING)
        self.assertEqual(resolved.location, self.remote_note.location)
        self.assertEqual(resolved.author_device_id, "dispatch-web-02")
        self.assertEqual(resolved.version, 4)  # max(2, 3) + 1
        self.assertEqual(resolved.lamport_ts, 9)  # max(8, 6) + 1
        self.assertIs(resolved.sync_state, SyncState.PENDING_PUSH)
        self.assertEqual(resolved.updated_at, "2026-11-19T01:05:00+00:00")

    def test_resolve_manual_merge(self) -> None:
        resolved = ConflictResolver.resolve(
            self.local_note,
            self.remote_note,
            ConflictStrategy.MANUAL_MERGE,
            merged_title="Hotspot 98.4 C + Crew Bravo Dispatched",
            merged_body="Field reading 98.4 C combined with Crew Bravo replacement bushing dispatch.",
        )
        self.assertEqual(resolved.note_id, "NOTE-SITE-001")
        self.assertEqual(resolved.title, "Hotspot 98.4 C + Crew Bravo Dispatched")
        self.assertEqual(
            resolved.body,
            "Field reading 98.4 C combined with Crew Bravo replacement bushing dispatch.",
        )
        self.assertEqual(resolved.site_code, self.local_note.site_code)
        self.assertEqual(resolved.severity, self.local_note.severity)
        self.assertEqual(resolved.location, self.local_note.location)
        self.assertEqual(resolved.author_device_id, self.local_note.author_device_id)
        self.assertFalse(resolved.deleted)
        self.assertEqual(resolved.version, 4)
        self.assertEqual(resolved.lamport_ts, 9)
        self.assertIs(resolved.sync_state, SyncState.PENDING_PUSH)
        self.assertTrue(bool(resolved.updated_at))

    def test_resolve_rejects_missing_merge_fields_invalid_strategy_and_mismatched_ids(self) -> None:
        # Missing or whitespace merged_title / merged_body
        for bad_title, bad_body in [
            (None, "Valid body"),
            ("", "Valid body"),
            ("   ", "Valid body"),
            ("Valid title", None),
            ("Valid title", ""),
            ("Valid title", " \n\t "),
        ]:
            with self.subTest(merged_title=bad_title, merged_body=bad_body):
                with self.assertRaises(InvalidConflictResolutionError) as ctx:
                    ConflictResolver.resolve(
                        self.local_note,
                        self.remote_note,
                        ConflictStrategy.MANUAL_MERGE,
                        merged_title=bad_title,
                        merged_body=bad_body,
                    )
                self.assertEqual(ctx.exception.code, "ERR-422-INVALID-RESOLUTION")

        # Unknown / invalid strategy
        for bad_strategy in ("LAST_WRITE_WINS", "", None, 42):
            with self.subTest(strategy=bad_strategy):
                with self.assertRaises(InvalidConflictResolutionError):
                    ConflictResolver.resolve(
                        self.local_note,
                        self.remote_note,
                        bad_strategy,  # type: ignore[arg-type]
                    )

        # Mismatched note IDs
        mismatched_remote = _make_note(note_id="NOTE-SITE-999", version=2, lamport_ts=9)
        with self.assertRaises(InvalidConflictResolutionError):
            ConflictResolver.resolve(
                self.local_note,
                mismatched_remote,
                ConflictStrategy.KEEP_LOCAL,
            )

        # Non-FieldNote arguments or invalid updated_at
        with self.assertRaises(InvalidConflictResolutionError):
            ConflictResolver.resolve(
                "not-a-note",  # type: ignore[arg-type]
                self.remote_note,
                ConflictStrategy.KEEP_LOCAL,
            )
        with self.assertRaises(InvalidConflictResolutionError):
            ConflictResolver.resolve(
                self.local_note,
                self.remote_note,
                ConflictStrategy.KEEP_LOCAL,
                updated_at=12345,  # type: ignore[arg-type]
            )


class TestDomainPurityAndMobileManifestHygiene(unittest.TestCase):
    """Enforce AST-level domain purity and mobile-app strict TypeScript hygiene."""

    ALLOWED_STDLIB_IMPORTS = {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "math",
        "threading",
        "typing",
    }

    FORBIDDEN_PREFIXES = (
        "sqlite3",
        "http",
        "fastapi",
        "urllib",
        "sqlalchemy",
        "psycopg",
        "psycopg2",
        "redis",
        "requests",
        "httpx",
        "src.infrastructure",
        "src.interfaces",
        "src.application",
    )

    def test_ast_domain_purity_zero_infrastructure_imports(self) -> None:
        domain_files = sorted(DOMAIN_DIR.glob("*.py"))
        self.assertGreaterEqual(
            len(domain_files),
            5,
            f"Expected at least 5 domain modules in {DOMAIN_DIR}, found {len(domain_files)}.",
        )

        violations: list[str] = []
        for path in domain_files:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                imported_modules: list[str] = []
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:
                        continue
                    if node.module:
                        imported_modules.append(node.module)

                for mod in imported_modules:
                    if any(mod == bad or mod.startswith(f"{bad}.") for bad in self.FORBIDDEN_PREFIXES):
                        violations.append(f"{path.name}: forbidden import '{mod}'")
                    elif mod not in self.ALLOWED_STDLIB_IMPORTS and not mod.startswith("src.domain"):
                        violations.append(f"{path.name}: non-allowlisted import '{mod}'")

        self.assertEqual(
            violations,
            [],
            f"Domain purity violations detected in src/domain/: {violations}",
        )

    def test_mobile_app_manifest_and_strict_typescript_hygiene(self) -> None:
        # Verify manifests parse cleanly as JSON
        for config_name in ("package.json", "app.json", "tsconfig.json"):
            config_path = MOBILE_APP_DIR / config_name
            self.assertTrue(config_path.exists(), f"Missing mobile-app config: {config_path}")
            json.loads(config_path.read_text(encoding="utf-8"))

        tsconfig = json.loads((MOBILE_APP_DIR / "tsconfig.json").read_text(encoding="utf-8"))
        compiler_options = tsconfig.get("compilerOptions", {})
        self.assertIs(
            compiler_options.get("strict"),
            True,
            "mobile-app/tsconfig.json must enforce compilerOptions.strict = true.",
        )

        mobile_src_dir = MOBILE_APP_DIR / "src"
        ts_files = sorted(
            [*mobile_src_dir.rglob("*.ts"), *mobile_src_dir.rglob("*.tsx")]
        )
        self.assertGreaterEqual(
            len(ts_files),
            4,
            f"Expected mobile-app TypeScript source files under {mobile_src_dir}.",
        )

        forbidden_any_re = re.compile(r":\s*any\b|\bas\s+any\b|<any>")
        offending: list[str] = []
        for ts_file in ts_files:
            content = ts_file.read_text(encoding="utf-8")
            for lineno, line in enumerate(content.splitlines(), start=1):
                if forbidden_any_re.search(line):
                    offending.append(f"{ts_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

        self.assertEqual(
            offending,
            [],
            f"Forbidden TypeScript 'any' usage found in mobile-app/src/: {offending}",
        )


if __name__ == "__main__":
    unittest.main()
