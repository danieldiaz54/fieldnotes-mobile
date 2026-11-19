"""Default realistic field inspection seed notes and synchronized store initializer."""

from __future__ import annotations

from typing import Any

from src.domain.notes import FieldNote, GeoCoordinate, InspectionSeverity, SyncState
from src.infrastructure.persistence.sqlite_sync_stores import (
    SQLiteDeviceStore,
    SQLiteRemoteSyncServerStore,
)

DEFAULT_FIELD_NOTES: list[dict[str, Any]] = [
    {
        "note_id": "NOTE-SITE-001",
        "site_code": "SUB-ANDES-01",
        "title": "High-Voltage Transformer Bushing Thermal Hotspot",
        "body": (
            "FLIR thermography recorded 94.2 C on Phase-B 230 kV bushing flange (+26 C over "
            "ambient reference). Oil level gauge nominal at 78%; scheduled de-energization "
            "and torque verification within 12 hours."
        ),
        "severity": InspectionSeverity.CRITICAL.value,
        "location": {"latitude": 4.7110, "longitude": -74.0721},
        "author_device_id": "device-ios-field-01",
        "version": 1,
        "lamport_ts": 1,
        "sync_state": SyncState.SYNCED.value,
        "updated_at": "2026-11-18T08:00:00+00:00",
        "deleted": False,
    },
    {
        "note_id": "NOTE-SITE-002",
        "site_code": "PIPE-VALVE-04",
        "title": "Mainline Block Valve Actuator Pressure Drift",
        "body": (
            "Hydraulic accumulator pressure drifted from 145 bar to 132 bar over 48 hours "
            "at Kilometer Post 42. No external seal weep observed; nitrogen pre-charge "
            "top-up queued for next maintenance window."
        ),
        "severity": InspectionSeverity.WARNING.value,
        "location": {"latitude": 6.2442, "longitude": -75.5812},
        "author_device_id": "device-ios-field-01",
        "version": 1,
        "lamport_ts": 2,
        "sync_state": SyncState.SYNCED.value,
        "updated_at": "2026-11-18T08:15:00+00:00",
        "deleted": False,
    },
    {
        "note_id": "NOTE-SITE-003",
        "site_code": "TURB-NORTH-07",
        "title": "Wind Turbine Gearbox Lubricant Sample Nominal",
        "body": (
            "Collected 250 mL ISO 4406 oil sample from main planetary stage at 41 C operating "
            "temperature. Magnetic chip detector completely clean and desiccant breather "
            "silica remains blue (below 15% moisture saturation)."
        ),
        "severity": InspectionSeverity.INFO.value,
        "location": {"latitude": 11.5444, "longitude": -72.9072},
        "author_device_id": "device-ios-field-01",
        "version": 1,
        "lamport_ts": 3,
        "sync_state": SyncState.SYNCED.value,
        "updated_at": "2026-11-18T08:30:00+00:00",
        "deleted": False,
    },
    {
        "note_id": "NOTE-SITE-004",
        "site_code": "COMP-STN-12",
        "title": "Centrifugal Compressor Outboard Bearing Vibration Alert",
        "body": (
            "Tri-axial accelerometer captured 9.8 mm/s RMS velocity peak at 2X shaft speed "
            "(ISO 10816 Zone D alarm threshold is 7.1 mm/s). Recommended immediate load "
            "reduction to 65% and laser shaft alignment check."
        ),
        "severity": InspectionSeverity.CRITICAL.value,
        "location": {"latitude": 7.1193, "longitude": -73.1227},
        "author_device_id": "device-ios-field-01",
        "version": 1,
        "lamport_ts": 4,
        "sync_state": SyncState.SYNCED.value,
        "updated_at": "2026-11-18T08:45:00+00:00",
        "deleted": False,
    },
    {
        "note_id": "NOTE-SITE-005",
        "site_code": "SOLAR-INV-09",
        "title": "PV Combiner String 4B Insulation Resistance Drop",
        "body": (
            "Megger test measured 1.4 MOhm DC-to-ground resistance following morning fog "
            "condensation (nominal baseline > 20 MOhm). MC4 home-run connectors inspected "
            "for UV jacket cracking; replacement conduit gland staged."
        ),
        "severity": InspectionSeverity.WARNING.value,
        "location": {"latitude": 3.4516, "longitude": -76.5320},
        "author_device_id": "device-ios-field-01",
        "version": 1,
        "lamport_ts": 5,
        "sync_state": SyncState.SYNCED.value,
        "updated_at": "2026-11-18T09:00:00+00:00",
        "deleted": False,
    },
]


def seed_synced_stores(
    device_store: SQLiteDeviceStore,
    remote_store: SQLiteRemoteSyncServerStore,
) -> int:
    """Seed 5 synchronized baseline field inspection notes identically into both stores."""
    count = 0
    for raw in DEFAULT_FIELD_NOTES:
        loc_raw = raw["location"]
        note = FieldNote(
            note_id=str(raw["note_id"]),
            site_code=str(raw["site_code"]),
            title=str(raw["title"]),
            body=str(raw["body"]),
            severity=InspectionSeverity(str(raw["severity"])),
            location=GeoCoordinate(
                latitude=float(loc_raw["latitude"]),
                longitude=float(loc_raw["longitude"]),
            ),
            author_device_id=str(raw["author_device_id"]),
            version=int(raw["version"]),
            lamport_ts=int(raw["lamport_ts"]),
            sync_state=SyncState.SYNCED,
            updated_at=str(raw["updated_at"]),
            deleted=bool(raw.get("deleted", False)),
        )
        remote_store.upsert_initial_note(note)
        device_store.save_synced_note(note)
        count += 1
    return count


__all__ = [
    "DEFAULT_FIELD_NOTES",
    "seed_synced_stores",
]
