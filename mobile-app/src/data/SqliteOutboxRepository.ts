/**
 * Expo SQLite Local-First Outbox Adapter & Typed Sync API Client.
 *
 * Persists field notes, transactional outbox mutations, unresolved conflicts, and an
 * append-only sync audit ledger inside on-device SQLite (`expo-sqlite` SDK 52 async API).
 */

import * as SQLite from "expo-sqlite";

import {
  ConflictRecord,
  ConflictStrategy,
  DeltaSyncResultDto,
  FieldNote,
  InspectionSeverity,
  MutationOperation,
  MutationStatus,
  OfflineNetworkError,
  PendingMutation,
  ResolveConflictRequestDto,
  ResolveConflictResponseDto,
  SyncAuditRecord,
  SyncState,
  validateFieldNote,
} from "../domain/FieldNote";

interface LocalNoteRow {
  readonly note_id: string;
  readonly site_code: string;
  readonly title: string;
  readonly body: string;
  readonly severity: InspectionSeverity;
  readonly latitude: number;
  readonly longitude: number;
  readonly author_device_id: string;
  readonly version: number;
  readonly lamport_ts: number;
  readonly sync_state: SyncState;
  readonly updated_at: string;
  readonly deleted: number;
}

interface PendingMutationRow {
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

interface ConflictRow {
  readonly conflict_id: string;
  readonly note_id: string;
  readonly local_note_json: string;
  readonly remote_note_json: string;
  readonly detected_at: string;
  readonly resolved: number;
  readonly strategy: ConflictStrategy | null;
}

interface MaxLamportRow {
  readonly highest_ts: number | null;
}

export interface LocalDeviceStorePort {
  initialize(): Promise<void>;
  listLocalNotes(): Promise<readonly FieldNote[]>;
  upsertLocalNoteOffline(
    note: FieldNote,
    baseVersion: number,
    operation?: MutationOperation,
  ): Promise<{ readonly note: FieldNote; readonly mutation: PendingMutation }>;
  listPendingMutations(onlyQueued?: boolean): Promise<readonly PendingMutation[]>;
  markMutationStatus(mutationId: string, status: MutationStatus): Promise<void>;
  saveSyncedNote(note: FieldNote): Promise<FieldNote>;
  recordConflict(conflict: ConflictRecord): Promise<void>;
  listConflicts(unresolvedOnly?: boolean): Promise<readonly ConflictRecord[]>;
  markConflictResolved(
    conflictId: string,
    strategy: ConflictStrategy,
    resolvedNote: FieldNote,
  ): Promise<void>;
  appendSyncAudit(record: SyncAuditRecord): Promise<void>;
  getMaxLamportTs(): Promise<number>;
}

const SCHEMA_SQL = `
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
`;

function mapNoteRow(row: LocalNoteRow): FieldNote {
  return validateFieldNote({
    note_id: row.note_id,
    site_code: row.site_code,
    title: row.title,
    body: row.body,
    severity: row.severity,
    location: {
      latitude: row.latitude,
      longitude: row.longitude,
    },
    author_device_id: row.author_device_id,
    version: row.version,
    lamport_ts: row.lamport_ts,
    sync_state: row.sync_state,
    updated_at: row.updated_at,
    deleted: row.deleted === 1,
  });
}

function generateLocalId(prefix: string): string {
  const entropy = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `${prefix}-${entropy}`;
}

export class SqliteOutboxRepository implements LocalDeviceStorePort {
  private readonly dbName: string;
  private readonly deviceId: string;
  private db: SQLite.SQLiteDatabase | null = null;

  public constructor(dbName = "fieldnotes_device.db", deviceId = "device-ios-field-01") {
    this.dbName = dbName;
    this.deviceId = deviceId;
  }

  private async getDatabase(): Promise<SQLite.SQLiteDatabase> {
    if (this.db === null) {
      this.db = await SQLite.openDatabaseAsync(this.dbName);
      await this.db.execAsync(SCHEMA_SQL);
    }
    return this.db;
  }

  public async initialize(): Promise<void> {
    await this.getDatabase();
  }

  public async listLocalNotes(): Promise<readonly FieldNote[]> {
    const db = await this.getDatabase();
    const rows = await db.getAllAsync<LocalNoteRow>(
      `SELECT note_id, site_code, title, body, severity, latitude, longitude,
              author_device_id, version, lamport_ts, sync_state, updated_at, deleted
       FROM local_notes
       ORDER BY note_id ASC`,
    );
    return rows.map(mapNoteRow);
  }

  public async upsertLocalNoteOffline(
    note: FieldNote,
    baseVersion: number,
    operation: MutationOperation = "UPSERT",
  ): Promise<{ readonly note: FieldNote; readonly mutation: PendingMutation }> {
    const db = await this.getDatabase();
    const nowIso = new Date().toISOString();
    const pendingNote = validateFieldNote({
      ...note,
      sync_state: "PENDING_PUSH",
      updated_at: note.updated_at || nowIso,
      deleted: operation === "DELETE" ? true : note.deleted,
    });

    const mutation: PendingMutation = Object.freeze({
      mutation_id: generateLocalId("mut"),
      note_id: pendingNote.note_id,
      device_id: this.deviceId,
      operation,
      base_version: baseVersion,
      lamport_ts: pendingNote.lamport_ts,
      payload_json: JSON.stringify(pendingNote),
      status: "QUEUED",
      created_at: nowIso,
    });

    await db.withTransactionAsync(async () => {
      await db.runAsync(
        `INSERT INTO local_notes (
           note_id, site_code, title, body, severity, latitude, longitude,
           author_device_id, version, lamport_ts, sync_state, updated_at, deleted
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(note_id) DO UPDATE SET
           site_code = excluded.site_code,
           title = excluded.title,
           body = excluded.body,
           severity = excluded.severity,
           latitude = excluded.latitude,
           longitude = excluded.longitude,
           author_device_id = excluded.author_device_id,
           version = excluded.version,
           lamport_ts = excluded.lamport_ts,
           sync_state = excluded.sync_state,
           updated_at = excluded.updated_at,
           deleted = excluded.deleted`,
        pendingNote.note_id,
        pendingNote.site_code,
        pendingNote.title,
        pendingNote.body,
        pendingNote.severity,
        pendingNote.location.latitude,
        pendingNote.location.longitude,
        pendingNote.author_device_id,
        pendingNote.version,
        pendingNote.lamport_ts,
        pendingNote.sync_state,
        pendingNote.updated_at,
        pendingNote.deleted ? 1 : 0,
      );

      await db.runAsync(
        `INSERT INTO pending_mutations (
           mutation_id, note_id, device_id, operation, base_version,
           lamport_ts, payload_json, status, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        mutation.mutation_id,
        mutation.note_id,
        mutation.device_id,
        mutation.operation,
        mutation.base_version,
        mutation.lamport_ts,
        mutation.payload_json,
        mutation.status,
        mutation.created_at,
      );
    });

    return { note: pendingNote, mutation };
  }

  public async listPendingMutations(onlyQueued = false): Promise<readonly PendingMutation[]> {
    const db = await this.getDatabase();
    const rows = onlyQueued
      ? await db.getAllAsync<PendingMutationRow>(
          `SELECT mutation_id, note_id, device_id, operation, base_version,
                  lamport_ts, payload_json, status, created_at
           FROM pending_mutations
           WHERE status = 'QUEUED'
           ORDER BY lamport_ts ASC, created_at ASC, mutation_id ASC`,
        )
      : await db.getAllAsync<PendingMutationRow>(
          `SELECT mutation_id, note_id, device_id, operation, base_version,
                  lamport_ts, payload_json, status, created_at
           FROM pending_mutations
           ORDER BY lamport_ts ASC, created_at ASC, mutation_id ASC`,
        );

    return rows.map((row) => Object.freeze({ ...row }));
  }

  public async markMutationStatus(mutationId: string, status: MutationStatus): Promise<void> {
    const db = await this.getDatabase();
    await db.runAsync(
      `UPDATE pending_mutations SET status = ? WHERE mutation_id = ?`,
      status,
      mutationId,
    );
  }

  public async saveSyncedNote(note: FieldNote): Promise<FieldNote> {
    const db = await this.getDatabase();
    const syncedNote = validateFieldNote({
      ...note,
      sync_state: "SYNCED",
      updated_at: note.updated_at || new Date().toISOString(),
    });
    await db.runAsync(
      `INSERT INTO local_notes (
         note_id, site_code, title, body, severity, latitude, longitude,
         author_device_id, version, lamport_ts, sync_state, updated_at, deleted
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(note_id) DO UPDATE SET
         site_code = excluded.site_code,
         title = excluded.title,
         body = excluded.body,
         severity = excluded.severity,
         latitude = excluded.latitude,
         longitude = excluded.longitude,
         author_device_id = excluded.author_device_id,
         version = excluded.version,
         lamport_ts = excluded.lamport_ts,
         sync_state = excluded.sync_state,
         updated_at = excluded.updated_at,
         deleted = excluded.deleted`,
      syncedNote.note_id,
      syncedNote.site_code,
      syncedNote.title,
      syncedNote.body,
      syncedNote.severity,
      syncedNote.location.latitude,
      syncedNote.location.longitude,
      syncedNote.author_device_id,
      syncedNote.version,
      syncedNote.lamport_ts,
      syncedNote.sync_state,
      syncedNote.updated_at,
      syncedNote.deleted ? 1 : 0,
    );
    return syncedNote;
  }

  public async recordConflict(conflict: ConflictRecord): Promise<void> {
    const db = await this.getDatabase();
    await db.withTransactionAsync(async () => {
      await db.runAsync(
        `INSERT INTO local_conflicts (
           conflict_id, note_id, local_note_json, remote_note_json,
           detected_at, resolved, strategy, resolved_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(conflict_id) DO UPDATE SET
           local_note_json = excluded.local_note_json,
           remote_note_json = excluded.remote_note_json,
           resolved = excluded.resolved,
           strategy = excluded.strategy`,
        conflict.conflict_id,
        conflict.note_id,
        JSON.stringify(conflict.local_note),
        JSON.stringify(conflict.remote_note),
        conflict.detected_at,
        conflict.resolved ? 1 : 0,
        conflict.strategy,
        conflict.resolved ? new Date().toISOString() : null,
      );

      if (!conflict.resolved) {
        await db.runAsync(
          `UPDATE local_notes SET sync_state = 'CONFLICT' WHERE note_id = ?`,
          conflict.note_id,
        );
      }
    });
  }

  public async listConflicts(unresolvedOnly = false): Promise<readonly ConflictRecord[]> {
    const db = await this.getDatabase();
    const rows = unresolvedOnly
      ? await db.getAllAsync<ConflictRow>(
          `SELECT conflict_id, note_id, local_note_json, remote_note_json,
                  detected_at, resolved, strategy
           FROM local_conflicts
           WHERE resolved = 0
           ORDER BY detected_at DESC`,
        )
      : await db.getAllAsync<ConflictRow>(
          `SELECT conflict_id, note_id, local_note_json, remote_note_json,
                  detected_at, resolved, strategy
           FROM local_conflicts
           ORDER BY detected_at DESC`,
        );

    return rows.map((row) =>
      Object.freeze({
        conflict_id: row.conflict_id,
        note_id: row.note_id,
        local_note: validateFieldNote(JSON.parse(row.local_note_json) as FieldNote),
        remote_note: validateFieldNote(JSON.parse(row.remote_note_json) as FieldNote),
        detected_at: row.detected_at,
        resolved: row.resolved === 1,
        strategy: row.strategy,
      }),
    );
  }

  public async markConflictResolved(
    conflictId: string,
    strategy: ConflictStrategy,
    resolvedNote: FieldNote,
  ): Promise<void> {
    const db = await this.getDatabase();
    const nowIso = new Date().toISOString();
    const validated = validateFieldNote(resolvedNote);
    await db.withTransactionAsync(async () => {
      await db.runAsync(
        `UPDATE local_conflicts
         SET resolved = 1, strategy = ?, resolved_at = ?
         WHERE conflict_id = ?`,
        strategy,
        nowIso,
        conflictId,
      );
      await db.runAsync(
        `UPDATE local_notes
         SET title = ?, body = ?, severity = ?, version = ?, lamport_ts = ?,
             sync_state = ?, updated_at = ?, deleted = ?
         WHERE note_id = ?`,
        validated.title,
        validated.body,
        validated.severity,
        validated.version,
        validated.lamport_ts,
        validated.sync_state,
        validated.updated_at,
        validated.deleted ? 1 : 0,
        validated.note_id,
      );
    });
  }

  public async appendSyncAudit(record: SyncAuditRecord): Promise<void> {
    const db = await this.getDatabase();
    await db.runAsync(
      `INSERT INTO sync_audit_ledger (
         audit_id, device_id, direction, pushed_count, pulled_count,
         conflicts_detected, status, created_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      record.audit_id,
      record.device_id,
      record.direction,
      record.pushed_count,
      record.pulled_count,
      record.conflicts_detected,
      record.status,
      record.created_at,
    );
  }

  public async getMaxLamportTs(): Promise<number> {
    const db = await this.getDatabase();
    const row = await db.getFirstAsync<MaxLamportRow>(
      `SELECT MAX(max_ts) AS highest_ts FROM (
         SELECT COALESCE(MAX(lamport_ts), 0) AS max_ts FROM local_notes
         UNION ALL
         SELECT COALESCE(MAX(lamport_ts), 0) AS max_ts FROM pending_mutations
       )`,
    );
    return row?.highest_ts ?? 0;
  }
}

export class FieldNotesSyncHttpClient {
  private readonly baseUrl: string;
  private readonly deviceId: string;

  public constructor(baseUrl = "http://127.0.0.1:8097", deviceId = "device-ios-field-01") {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.deviceId = deviceId;
  }

  public async triggerDeltaSync(isOnline: boolean): Promise<DeltaSyncResultDto> {
    if (!isOnline) {
      throw new OfflineNetworkError(
        "Device is offline; mutations remain safely queued in local SQLite outbox",
        this.deviceId,
      );
    }
    const response = await fetch(`${this.baseUrl}/api/v1/sync/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (response.status === 503) {
      throw new OfflineNetworkError(
        "Remote sync endpoint returned 503 Service Unavailable (offline).",
        this.deviceId,
      );
    }
    if (!response.ok) {
      throw new Error(`Delta sync failed with HTTP ${String(response.status)}.`);
    }
    return (await response.json()) as DeltaSyncResultDto;
  }

  public async resolveConflict(
    conflictId: string,
    request: ResolveConflictRequestDto,
  ): Promise<ResolveConflictResponseDto> {
    const response = await fetch(
      `${this.baseUrl}/api/v1/conflicts/${encodeURIComponent(conflictId)}/resolve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      },
    );
    if (response.status === 503) {
      throw new OfflineNetworkError(
        "Cannot push conflict resolution while device is offline.",
        this.deviceId,
      );
    }
    if (!response.ok) {
      throw new Error(`Conflict resolution failed with HTTP ${String(response.status)}.`);
    }
    return (await response.json()) as ResolveConflictResponseDto;
  }
}
