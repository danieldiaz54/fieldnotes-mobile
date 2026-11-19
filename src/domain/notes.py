"""Immutable, self-validating local-first field inspection domain models.

Zero external or infrastructure dependencies: uses Python 3.12 standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import math
from typing import Any

from src.domain.exceptions import InvalidFieldNoteError


class InspectionSeverity(str, Enum):
    """Operational severity classification for a field inspection note."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SyncState(str, Enum):
    """Synchronization lifecycle state of a local field note."""

    SYNCED = "SYNCED"
    PENDING_PUSH = "PENDING_PUSH"
    CONFLICT = "CONFLICT"


class ConflictStrategy(str, Enum):
    """Explicit deterministic strategy for resolving diverging note versions."""

    KEEP_LOCAL = "KEEP_LOCAL"
    ACCEPT_REMOTE = "ACCEPT_REMOTE"
    MANUAL_MERGE = "MANUAL_MERGE"


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


def _require_finite_coordinate(
    value: object,
    field_name: str,
    *,
    low: float,
    high: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFieldNoteError(
            f"{field_name} must be a finite non-bool number; got {value!r}."
        )
    numeric = float(value)
    if not math.isfinite(numeric):
        raise InvalidFieldNoteError(
            f"{field_name} must be finite (not NaN or infinity); got {value!r}."
        )
    if numeric < low or numeric > high:
        raise InvalidFieldNoteError(
            f"{field_name} must be within [{low}, {high}]; got {numeric}."
        )
    return numeric


def _coerce_severity(value: object) -> InspectionSeverity:
    if isinstance(value, InspectionSeverity):
        return value
    if isinstance(value, str):
        try:
            return InspectionSeverity(value)
        except ValueError as exc:
            raise InvalidFieldNoteError(
                f"FieldNote.severity must be a valid InspectionSeverity; got {value!r}."
            ) from exc
    raise InvalidFieldNoteError(
        f"FieldNote.severity must be an InspectionSeverity; got {value!r}."
    )


def _coerce_sync_state(value: object) -> SyncState:
    if isinstance(value, SyncState):
        return value
    if isinstance(value, str):
        try:
            return SyncState(value)
        except ValueError as exc:
            raise InvalidFieldNoteError(
                f"FieldNote.sync_state must be a valid SyncState; got {value!r}."
            ) from exc
    raise InvalidFieldNoteError(
        f"FieldNote.sync_state must be a SyncState; got {value!r}."
    )


@dataclass(frozen=True)
class GeoCoordinate:
    """Immutable WGS84 geographic coordinate value object."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        lat = _require_finite_coordinate(
            self.latitude,
            "GeoCoordinate.latitude",
            low=-90.0,
            high=90.0,
        )
        lon = _require_finite_coordinate(
            self.longitude,
            "GeoCoordinate.longitude",
            low=-180.0,
            high=180.0,
        )
        object.__setattr__(self, "latitude", lat)
        object.__setattr__(self, "longitude", lon)

    def to_dict(self) -> dict[str, float]:
        """Serialize coordinate into a plain dictionary."""
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GeoCoordinate:
        """Reconstruct and validate a GeoCoordinate from a mapping."""
        if not isinstance(data, Mapping):
            raise InvalidFieldNoteError("GeoCoordinate payload must be a mapping.")
        if "latitude" not in data or "longitude" not in data:
            raise InvalidFieldNoteError(
                "GeoCoordinate payload must include 'latitude' and 'longitude'."
            )
        return cls(latitude=data["latitude"], longitude=data["longitude"])


@dataclass(frozen=True)
class FieldNote:
    """Immutable, self-validating local-first field inspection note aggregate."""

    note_id: str
    site_code: str
    title: str
    body: str
    severity: InspectionSeverity
    location: GeoCoordinate
    author_device_id: str
    version: int = 1
    lamport_ts: int = 1
    sync_state: SyncState = SyncState.SYNCED
    updated_at: str = ""
    deleted: bool = False

    def __post_init__(self) -> None:
        _require_non_empty_str(self.note_id, "FieldNote.note_id")
        _require_non_empty_str(self.site_code, "FieldNote.site_code")
        _require_non_empty_str(self.title, "FieldNote.title")
        _require_non_empty_str(self.body, "FieldNote.body")
        _require_non_empty_str(self.author_device_id, "FieldNote.author_device_id")

        coerced_severity = _coerce_severity(self.severity)
        object.__setattr__(self, "severity", coerced_severity)

        if not isinstance(self.location, GeoCoordinate):
            raise InvalidFieldNoteError(
                f"FieldNote.location must be a GeoCoordinate; got {type(self.location).__name__}."
            )

        _require_int_at_least(self.version, "FieldNote.version", minimum=1)
        _require_int_at_least(self.lamport_ts, "FieldNote.lamport_ts", minimum=1)

        coerced_sync_state = _coerce_sync_state(self.sync_state)
        object.__setattr__(self, "sync_state", coerced_sync_state)

        if not isinstance(self.updated_at, str):
            raise InvalidFieldNoteError(
                f"FieldNote.updated_at must be a string; got {type(self.updated_at).__name__}."
            )

        if not isinstance(self.deleted, bool):
            raise InvalidFieldNoteError(
                f"FieldNote.deleted must be a bool; got {type(self.deleted).__name__}."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize field note into a JSON-compatible plain dictionary."""
        return {
            "note_id": self.note_id,
            "site_code": self.site_code,
            "title": self.title,
            "body": self.body,
            "severity": self.severity.value,
            "location": self.location.to_dict(),
            "author_device_id": self.author_device_id,
            "version": self.version,
            "lamport_ts": self.lamport_ts,
            "sync_state": self.sync_state.value,
            "updated_at": self.updated_at,
            "deleted": self.deleted,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FieldNote:
        """Reconstruct and validate a FieldNote from a mapping."""
        if not isinstance(data, Mapping):
            raise InvalidFieldNoteError("FieldNote payload must be a mapping.")
        required_fields = (
            "note_id",
            "site_code",
            "title",
            "body",
            "severity",
            "location",
            "author_device_id",
        )
        missing = [field for field in required_fields if field not in data]
        if missing:
            raise InvalidFieldNoteError(
                f"FieldNote payload missing required fields: {', '.join(missing)}."
            )
        raw_location = data["location"]
        location = (
            raw_location
            if isinstance(raw_location, GeoCoordinate)
            else GeoCoordinate.from_dict(raw_location)
        )
        return cls(
            note_id=data["note_id"],
            site_code=data["site_code"],
            title=data["title"],
            body=data["body"],
            severity=data["severity"],
            location=location,
            author_device_id=data["author_device_id"],
            version=data.get("version", 1),
            lamport_ts=data.get("lamport_ts", 1),
            sync_state=data.get("sync_state", SyncState.SYNCED),
            updated_at=data.get("updated_at", ""),
            deleted=data.get("deleted", False),
        )


__all__ = [
    "ConflictStrategy",
    "FieldNote",
    "GeoCoordinate",
    "InspectionSeverity",
    "SyncState",
]
