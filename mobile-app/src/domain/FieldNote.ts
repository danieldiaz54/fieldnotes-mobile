/**
 * Pure Domain Models, Typed Exceptions, and REST Sync DTO Contracts for FieldNotes Mobile.
 *
 * Mirrors `src/domain/notes.py`, `src/domain/sync_engine.py`, and
 * `.specs/03-integration/offline-sync-api-and-expo-app.spec.md` with strict TypeScript types
 * and zero external dependencies.
 */

export type InspectionSeverity = "INFO" | "WARNING" | "CRITICAL";

export type SyncState = "SYNCED" | "PENDING_PUSH" | "CONFLICT";

export type ConflictStrategy = "KEEP_LOCAL" | "ACCEPT_REMOTE" | "MANUAL_MERGE";

export type MutationOperation = "UPSERT" | "DELETE";

export type MutationStatus = "QUEUED" | "SYNCED" | "CONFLICT";

export interface GeoCoordinate {
  readonly latitude: number;
  readonly longitude: number;
}

export interface FieldNote {
  readonly note_id: string;
  readonly site_code: string;
  readonly title: string;
  readonly body: string;
  readonly severity: InspectionSeverity;
  readonly location: GeoCoordinate;
  readonly author_device_id: string;
  readonly version: number;
  readonly lamport_ts: number;
  readonly sync_state: SyncState;
  readonly updated_at: string;
  readonly deleted: boolean;
}

export interface PendingMutation {
  readonly mutation_id: string;
  readonly note_id: string;
  readonly device_id: string;
  readonly operation: MutationOperation;
  readonly base_version: number;
  readonly lamport_ts: number;
  readonly payload_json: string;
  readonly status: MutationStatus;
  readonly created_at: string;
}

export interface ConflictRecord {
  readonly conflict_id: string;
  readonly note_id: string;
  readonly local_note: FieldNote;
  readonly remote_note: FieldNote;
  readonly detected_at: string;
  readonly resolved: boolean;
  readonly strategy: ConflictStrategy | null;
}

export interface SyncAuditRecord {
  readonly audit_id: string;
  readonly device_id: string;
  readonly direction: string;
  readonly pushed_count: number;
  readonly pulled_count: number;
  readonly conflicts_detected: number;
  readonly status: string;
  readonly created_at: string;
}

export interface UpsertLocalNoteResponseDto {
  readonly note: FieldNote;
  readonly mutation: PendingMutation;
  readonly is_online: boolean;
}

export interface DeltaSyncResultDto {
  readonly pushed_count: number;
  readonly pulled_count: number;
  readonly conflicts_detected: number;
  readonly audit: SyncAuditRecord;
  readonly conflicts: readonly ConflictRecord[];
  readonly lamport_ts: number;
}

export interface ResolveConflictRequestDto {
  readonly strategy: ConflictStrategy;
  readonly merged_title?: string;
  readonly merged_body?: string;
}

export interface ResolveConflictResponseDto {
  readonly conflict: ConflictRecord;
  readonly resolved_note: FieldNote;
  readonly audit: SyncAuditRecord;
}

export interface ApiErrorEnvelopeDto {
  readonly error: {
    readonly code: string;
    readonly type: string;
    readonly message: string;
    readonly details: Readonly<Record<string, string | number | boolean | null | readonly string[]>>;
  };
}

export class DomainError extends Error {
  public readonly code: string;

  public constructor(message: string, code = "ERR-500-DOMAIN-ERROR") {
    super(message);
    this.name = new.target.name;
    this.code = code;
  }
}

export class InvalidFieldNoteError extends DomainError {
  public constructor(message: string) {
    super(message, "ERR-400-INVALID-FIELD-NOTE");
  }
}

export class InvalidConflictResolutionError extends DomainError {
  public constructor(message: string) {
    super(message, "ERR-422-INVALID-RESOLUTION");
  }
}

export class OfflineNetworkError extends DomainError {
  public readonly deviceId: string | null;

  public constructor(
    message = "Device is offline; mutations remain safely queued in local SQLite outbox",
    deviceId: string | null = null,
  ) {
    super(message, "ERR-503-DEVICE-OFFLINE");
    this.deviceId = deviceId;
  }
}

export const VALID_SEVERITIES: readonly InspectionSeverity[] = ["INFO", "WARNING", "CRITICAL"];
export const VALID_SYNC_STATES: readonly SyncState[] = ["SYNCED", "PENDING_PUSH", "CONFLICT"];
export const VALID_CONFLICT_STRATEGIES: readonly ConflictStrategy[] = [
  "KEEP_LOCAL",
  "ACCEPT_REMOTE",
  "MANUAL_MERGE",
];

export function validateGeoCoordinate(coord: GeoCoordinate): GeoCoordinate {
  if (!Number.isFinite(coord.latitude) || coord.latitude < -90 || coord.latitude > 90) {
    throw new InvalidFieldNoteError(
      `GeoCoordinate.latitude must be a finite number in [-90, 90]; got ${String(coord.latitude)}.`,
    );
  }
  if (!Number.isFinite(coord.longitude) || coord.longitude < -180 || coord.longitude > 180) {
    throw new InvalidFieldNoteError(
      `GeoCoordinate.longitude must be a finite number in [-180, 180]; got ${String(coord.longitude)}.`,
    );
  }
  return Object.freeze({ latitude: coord.latitude, longitude: coord.longitude });
}

export function validateFieldNote(note: FieldNote): FieldNote {
  if (!note.note_id.trim()) {
    throw new InvalidFieldNoteError("FieldNote.note_id must be a non-empty string.");
  }
  if (!note.site_code.trim()) {
    throw new InvalidFieldNoteError("FieldNote.site_code must be a non-empty string.");
  }
  if (!note.title.trim()) {
    throw new InvalidFieldNoteError("FieldNote.title must be a non-empty string.");
  }
  if (!note.body.trim()) {
    throw new InvalidFieldNoteError("FieldNote.body must be a non-empty string.");
  }
  if (!note.author_device_id.trim()) {
    throw new InvalidFieldNoteError("FieldNote.author_device_id must be a non-empty string.");
  }
  if (!VALID_SEVERITIES.includes(note.severity)) {
    throw new InvalidFieldNoteError(`Invalid severity: ${String(note.severity)}.`);
  }
  if (!VALID_SYNC_STATES.includes(note.sync_state)) {
    throw new InvalidFieldNoteError(`Invalid sync_state: ${String(note.sync_state)}.`);
  }
  if (!Number.isInteger(note.version) || note.version < 1) {
    throw new InvalidFieldNoteError(
      `FieldNote.version must be an integer >= 1; got ${String(note.version)}.`,
    );
  }
  if (!Number.isInteger(note.lamport_ts) || note.lamport_ts < 1) {
    throw new InvalidFieldNoteError(
      `FieldNote.lamport_ts must be an integer >= 1; got ${String(note.lamport_ts)}.`,
    );
  }
  const location = validateGeoCoordinate(note.location);
  return Object.freeze({ ...note, location });
}
