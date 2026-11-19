-- =============================================================================
-- Migration: 001_init_fieldnotes_sync.sql
-- Repository: fieldnotes-mobile
-- Target Engine: PostgreSQL 16+
-- Specification: .specs/02-data/local-sqlite-outbox-and-sync-audit-ledger.spec.md
-- Description:
--   1. Creates authoritative remote table `remote_notes` with optimistic
--      versioning (`version`) and causal Lamport timestamp (`lamport_ts`).
--   2. Creates immutable append-only audit ledger `sync_audit_ledger`.
--   3. Installs PL/pgSQL trigger function `prevent_sync_audit_mutation()` on
--      `sync_audit_ledger` (BEFORE UPDATE & BEFORE DELETE) raising
--      `APPEND_ONLY_LEDGER_VIOLATION`.
-- =============================================================================

BEGIN;

-- 1. Authoritative Remote Field Inspection Notes (`remote_notes`)
CREATE TABLE IF NOT EXISTS remote_notes (
    note_id          TEXT PRIMARY KEY,
    site_code        TEXT NOT NULL CHECK (length(trim(site_code)) > 0),
    title            TEXT NOT NULL CHECK (length(trim(title)) > 0),
    body             TEXT NOT NULL CHECK (length(trim(body)) > 0),
    severity         TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
    latitude         DOUBLE PRECISION NOT NULL CHECK (latitude >= -90.0 AND latitude <= 90.0),
    longitude        DOUBLE PRECISION NOT NULL CHECK (longitude >= -180.0 AND longitude <= 180.0),
    author_device_id TEXT NOT NULL CHECK (length(trim(author_device_id)) > 0),
    version          BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    lamport_ts       BIGINT NOT NULL DEFAULT 1 CHECK (lamport_ts >= 1),
    sync_state       TEXT NOT NULL DEFAULT 'SYNCED' CHECK (sync_state IN ('SYNCED', 'PENDING_PUSH', 'CONFLICT')),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted          BOOLEAN NOT NULL DEFAULT FALSE
);

-- Delta-pull index for causal Lamport watermark queries (`WHERE lamport_ts > $1`)
CREATE INDEX IF NOT EXISTS idx_remote_notes_lamport_ts
    ON remote_notes (lamport_ts ASC, note_id ASC);

-- Site-level lookup index for dispatch dashboards
CREATE INDEX IF NOT EXISTS idx_remote_notes_site_code
    ON remote_notes (site_code, updated_at DESC);

-- 2. Append-Only Immutable Sync Audit Ledger (`sync_audit_ledger`)
CREATE TABLE IF NOT EXISTS sync_audit_ledger (
    audit_id           TEXT PRIMARY KEY,
    device_id          TEXT NOT NULL CHECK (length(trim(device_id)) > 0),
    direction          TEXT NOT NULL CHECK (length(trim(direction)) > 0),
    pushed_count       INTEGER NOT NULL CHECK (pushed_count >= 0),
    pulled_count       INTEGER NOT NULL CHECK (pulled_count >= 0),
    conflicts_detected INTEGER NOT NULL CHECK (conflicts_detected >= 0),
    status             TEXT NOT NULL CHECK (length(trim(status)) > 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sync_audit_ledger_created_at
    ON sync_audit_ledger (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_audit_ledger_device_id
    ON sync_audit_ledger (device_id, created_at DESC);

-- 3. Database-Enforced Append-Only Immutability Guard (`prevent_sync_audit_mutation`)
CREATE OR REPLACE FUNCTION prevent_sync_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'APPEND_ONLY_LEDGER_VIOLATION: Sync audit ledger is strictly append-only (attempted % on audit_id=%)',
        TG_OP, OLD.audit_id
        USING ERRCODE = 'check_violation';
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_sync_audit_ledger_no_update'
          AND tgrelid = 'sync_audit_ledger'::regclass
    ) THEN
        CREATE TRIGGER trg_sync_audit_ledger_no_update
            BEFORE UPDATE ON sync_audit_ledger
            FOR EACH ROW
            EXECUTE FUNCTION prevent_sync_audit_mutation();
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_sync_audit_ledger_no_delete'
          AND tgrelid = 'sync_audit_ledger'::regclass
    ) THEN
        CREATE TRIGGER trg_sync_audit_ledger_no_delete
            BEFORE DELETE ON sync_audit_ledger
            FOR EACH ROW
            EXECUTE FUNCTION prevent_sync_audit_mutation();
    END IF;
END;
$$;

COMMIT;
