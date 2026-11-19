# SPEC-02: Local-First SQLite Outbox, Remote Cloud Sync Store & Append-Only Sync Audit Ledger

- **Bounded Context**: `fieldnotes-mobile` — Persistence & Offline-First Data Architecture (`src/infrastructure/persistence/`, `migrations/`)
- **Status**: Approved (SDD-First)
- **Author Role**: `data-architect`
- **Aligned Domain Specification**: `.specs/01-domain/offline-first-sync-and-conflict-resolution.spec.md`
- **Target Environments**:
  - **Development / CI (`FIELDNOTES_ENV=development`)**: Local on-device SQLite (`SQLiteDeviceStore`), local SQLite remote-sync simulator (`SQLiteRemoteSyncServerStore`), or isolated Dockerized PostgreSQL 16 (`postgres:16-alpine` via `docker-compose.dev.yml` / `.env.development`).
  - **Production (`FIELDNOTES_ENV=production`)**: On-device SQLite local store syncing to managed PostgreSQL 16+ cloud sync cluster (`.env.production`).

---

## 1. Architectural Objectives & Data Governance Rules

1. **ACID Local-First Write + Outbox Queueing (`local_notes` + `pending_mutations`)**:
   - Field technicians operating in disconnected or intermittent-connectivity zones write directly to `local_notes`.
   - `SQLiteDeviceStore.upsert_local_note_offline(note, base_version, operation=MutationOperation.UPSERT)` executes in a **single ACID SQLite transaction**:
     1. Upserts the `FieldNote` into `local_notes` with `sync_state = 'PENDING_PUSH'`.
     2. Appends a corresponding `PendingMutation` into `pending_mutations` with `status = 'QUEUED'`, `device_id`, `base_version`, `lamport_ts`, and serialized `payload_json`.
   - Initial seed / remote pull writes (`save_synced_note`) update `local_notes` directly with `sync_state = 'SYNCED'` without enqueueing a mutation.
2. **Optimistic Concurrency Control & Conflict Capture (`remote_notes` + `local_conflicts`)**:
   - `SQLiteRemoteSyncServerStore.push_mutation(note, base_version)` enforces optimistic version checking atomically:
     - Absent remotely (`note_id` not in `remote_notes`): inserts at `version = 1`, `sync_state = 'SYNCED'`, returning `(True, saved_note)`.
     - Clean fast-forward (`remote.version == base_version`): updates to `version = base_version + 1` and `lamport_ts = max(remote.lamport_ts, note.lamport_ts) + 1`, `sync_state = 'SYNCED'`, returning `(True, saved_note)`.
     - Divergence (`remote.version != base_version`): rejects without overwriting remote state, returning `(False, remote)`.
   - Rejected pushes are persisted locally in `local_conflicts` via `record_conflict(conflict)`, atomically updating `local_notes.sync_state = 'CONFLICT'`.
   - Resolving a conflict via `mark_conflict_resolved(conflict_id, strategy, resolved_note)` atomically marks `local_conflicts.resolved = 1`, records `strategy` and `resolved_at`, and persists `resolved_note` in `local_notes`.
3. **Database-Enforced Append-Only Immutability (`sync_audit_ledger` & `remote_sync_audit_ledger`)**:
   - Every sync cycle outcome (`SyncAuditRecord`) is recorded in an **immutable, append-only ledger**.
   - **SQLite Triggers**: Both `sync_audit_ledger` (`SQLiteDeviceStore`) and `remote_sync_audit_ledger` (`SQLiteRemoteSyncServerStore`) enforce `BEFORE UPDATE` and `BEFORE DELETE` triggers calling:
     `SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');`
   - **PostgreSQL PL/pgSQL Triggers**: `migrations/001_init_fieldnotes_sync.sql` installs `prevent_sync_audit_mutation()` on `sync_audit_ledger` (`BEFORE UPDATE` and `BEFORE DELETE`), raising `APPEND_ONLY_LEDGER_VIOLATION` (`ERRCODE = 'check_violation'`).
   - Python stores map `APPEND_ONLY_LEDGER_VIOLATION` trigger aborts to `AppendOnlyLedgerError(RuntimeError)`.
4. **Repository Inversion**:
   - `SQLiteDeviceStore` and `SQLiteRemoteSyncServerStore` live strictly in `src/infrastructure/persistence/`, satisfy `LocalDeviceStoreProtocol` and `RemoteSyncStoreProtocol`, and return pure frozen domain entities (`FieldNote`, `GeoCoordinate`, `PendingMutation`, `ConflictRecord`, `SyncAuditRecord`) without leaking `sqlite3.Row` or SQL cursors into `src/domain/`.
5. **Strict Environment Isolation**:
   - Development reads exclusively from `.env.development` (`FIELDNOTES_ENV=development`, `FIELDNOTES_PORT=8097`, `LOCAL_SQLITE_PATH`, `POSTGRES_URL=postgresql://fieldnotes_dev:...@localhost:5442/fieldnotes_sync_dev`).
   - Production credentials live exclusively in `.env.production` (gitignored, placeholder tokens only in `.env.production.example`, `?sslmode=require`).

---

## 2. Relational Schema Design

```mermaid
erDiagram
    LOCAL_NOTES {
        text note_id PK
        text site_code
        text title
        text body
        text severity
        real latitude
        real longitude
        text author_device_id
        int version
        int lamport_ts
        text sync_state
        text updated_at
        int deleted
    }

    PENDING_MUTATIONS {
        text mutation_id PK
        text note_id
        text device_id
        text operation
        int base_version
        int lamport_ts
        text payload_json
        text status
        text created_at
    }

    LOCAL_CONFLICTS {
        text conflict_id PK
        text note_id
        text local_note_json
        text remote_note_json
        text detected_at
        int resolved
        text strategy
        text resolved_at
    }

    SYNC_AUDIT_LEDGER {
        text audit_id PK
        text device_id
        text direction
        int pushed_count
        int pulled_count
        int conflicts_detected
        text status
        text created_at
    }

    REMOTE_NOTES {
        text note_id PK
        text site_code
        text title
        text body
        text severity
        double latitude
        double longitude
        text author_device_id
        bigint version
        bigint lamport_ts
        text sync_state
        timestamptz updated_at
        boolean deleted
    }

    REMOTE_SYNC_AUDIT_LEDGER {
        text audit_id PK
        text device_id
        text direction
        int pushed_count
        int pulled_count
        int conflicts_detected
        text status
        text created_at
    }

    LOCAL_NOTES ||--o{ PENDING_MUTATIONS : "queues offline mutations"
    LOCAL_NOTES ||--o{ LOCAL_CONFLICTS : "tracks version conflicts"
```

### 2.1 Local-First SQLite Schema (`SQLiteDeviceStore`)

#### `local_notes`
- **Primary Key**: `note_id` (`TEXT`).
- **Columns**:
  - `site_code` (`TEXT NOT NULL`)
  - `title` (`TEXT NOT NULL`)
  - `body` (`TEXT NOT NULL`)
  - `severity` (`TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL'))`)
  - `latitude` (`REAL NOT NULL CHECK (latitude >= -90.0 AND latitude <= 90.0)`)
  - `longitude` (`REAL NOT NULL CHECK (longitude >= -180.0 AND longitude <= 180.0)`)
  - `author_device_id` (`TEXT NOT NULL`)
  - `version` (`INTEGER NOT NULL CHECK (version >= 1)`)
  - `lamport_ts` (`INTEGER NOT NULL CHECK (lamport_ts >= 1)`)
  - `sync_state` (`TEXT NOT NULL CHECK (sync_state IN ('SYNCED', 'PENDING_PUSH', 'CONFLICT'))`)
  - `updated_at` (`TEXT NOT NULL`)
  - `deleted` (`INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))`)
- **Indexes**: `idx_local_notes_sync_state (sync_state, lamport_ts ASC)`.

#### `pending_mutations` (Transactional Outbox Queue)
- **Primary Key**: `mutation_id` (`TEXT`).
- **Columns**:
  - `note_id` (`TEXT NOT NULL`)
  - `device_id` (`TEXT NOT NULL`)
  - `operation` (`TEXT NOT NULL CHECK (operation IN ('UPSERT', 'DELETE'))`)
  - `base_version` (`INTEGER NOT NULL CHECK (base_version >= 0)`)
  - `lamport_ts` (`INTEGER NOT NULL CHECK (lamport_ts >= 1)`)
  - `payload_json` (`TEXT NOT NULL`) — canonical JSON snapshot from `FieldNote.to_dict()`.
  - `status` (`TEXT NOT NULL CHECK (status IN ('QUEUED', 'SYNCED', 'CONFLICT'))`)
  - `created_at` (`TEXT NOT NULL`)
- **Causal Ordering**: Queries order by `lamport_ts ASC, created_at ASC, mutation_id ASC`.
- **Indexes**: `idx_pending_mutations_status_lamport (status, lamport_ts ASC, created_at ASC)`.

#### `local_conflicts`
- **Primary Key**: `conflict_id` (`TEXT`).
- **Columns**:
  - `note_id` (`TEXT NOT NULL`)
  - `local_note_json` (`TEXT NOT NULL`) — serialized local `FieldNote`.
  - `remote_note_json` (`TEXT NOT NULL`) — serialized authoritative remote `FieldNote`.
  - `detected_at` (`TEXT NOT NULL`)
  - `resolved` (`INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1))`)
  - `strategy` (`TEXT CHECK (strategy IS NULL OR strategy IN ('KEEP_LOCAL', 'ACCEPT_REMOTE', 'MANUAL_MERGE'))`)
  - `resolved_at` (`TEXT`)
- **Indexes**: `idx_local_conflicts_resolved (resolved, detected_at DESC)`.

#### `sync_audit_ledger` (Append-Only Immutable Audit Ledger)
- **Primary Key**: `audit_id` (`TEXT`).
- **Columns**:
  - `device_id` (`TEXT NOT NULL`)
  - `direction` (`TEXT NOT NULL`)
  - `pushed_count` (`INTEGER NOT NULL CHECK (pushed_count >= 0)`)
  - `pulled_count` (`INTEGER NOT NULL CHECK (pulled_count >= 0)`)
  - `conflicts_detected` (`INTEGER NOT NULL CHECK (conflicts_detected >= 0)`)
  - `status` (`TEXT NOT NULL`)
  - `created_at` (`TEXT NOT NULL`)
- **Immutability Triggers**:
  - `trg_sync_audit_ledger_no_update`: `BEFORE UPDATE ON sync_audit_ledger` -> `SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');`
  - `trg_sync_audit_ledger_no_delete`: `BEFORE DELETE ON sync_audit_ledger` -> `SELECT RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only');`

### 2.2 Remote Cloud Sync Schema (`SQLiteRemoteSyncServerStore` & PostgreSQL `001_init_fieldnotes_sync.sql`)

#### `remote_notes`
- Authoritative cloud table storing identical `FieldNote` columns (`note_id`, `site_code`, `title`, `body`, `severity`, `latitude`, `longitude`, `author_device_id`, `version`, `lamport_ts`, `sync_state`, `updated_at`, `deleted`).
- **Delta Pull Index**: `idx_remote_notes_lamport_ts (lamport_ts ASC)` powers `pull_delta_since(since_lamport_ts)`.

#### `remote_sync_audit_ledger` (SQLite Remote Store) / `sync_audit_ledger` (PostgreSQL Migration)
- Identical `SyncAuditRecord` columns (`audit_id`, `device_id`, `direction`, `pushed_count`, `pulled_count`, `conflicts_detected`, `status`, `created_at`).
- **Immutability Triggers**:
  - SQLite (`SQLiteRemoteSyncServerStore`): `trg_remote_sync_audit_ledger_no_update` and `trg_remote_sync_audit_ledger_no_delete` raising `APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only`.
  - PostgreSQL (`migrations/001_init_fieldnotes_sync.sql`): `prevent_sync_audit_mutation()` attached `BEFORE UPDATE` and `BEFORE DELETE` on `sync_audit_ledger` raising `APPEND_ONLY_LEDGER_VIOLATION`.

---

## 3. Environment Isolation Matrix

| Variable | `.env.development.example` | `.env.production.example` | Purpose |
| :--- | :--- | :--- | :--- |
| `FIELDNOTES_ENV` | `development` | `production` | Runtime environment selector |
| `FIELDNOTES_PORT` | `8097` | `8097` | HTTP sync engine port |
| `LOCAL_SQLITE_PATH` | `./.data/fieldnotes_device_dev.sqlite3` | *(mobile sandbox path)* | On-device local-first SQLite database path |
| `POSTGRES_URL` | `postgresql://fieldnotes_dev:fieldnotes_dev_local@localhost:5442/fieldnotes_sync_dev` | `postgresql://__PROD_POSTGRES_USER__:__PROD_POSTGRES_PASSWORD__@__PROD_POSTGRES_HOST__:5432/fieldnotes_sync_prod?sslmode=require` | Remote cloud sync PostgreSQL 16 connection URI |

---

## 4. Migration & Rollback Discipline

- **Forward Migration**: `migrations/001_init_fieldnotes_sync.sql` initializes `remote_notes`, `sync_audit_ledger`, operational delta-pull/audit indexes, and the `prevent_sync_audit_mutation()` PL/pgSQL append-only guard function and triggers.
- **Idempotency**: Uses `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`, and `pg_trigger` existence checks so repeated initialization on container startup is safe.
- **Rollback Consideration**:
  - `sync_audit_ledger` MUST NEVER be dropped or truncated in staging/production without an archived, verified compliance snapshot.
  - Index changes on `remote_notes` (`idx_remote_notes_lamport_ts`, `idx_remote_notes_site_code`) should use `CREATE INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY` in production to avoid blocking concurrent field sync pushes.
