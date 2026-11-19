# Agentic Architecture Changelog — fieldnotes-mobile

## [2026-11-18] - Release v0.1.0: Clean Architecture Offline-First Sync Engine, Expo TypeScript App & Append-Only Audit

### Added
- **SDD Specifications (`.specs/`)**:
  - `.specs/01-domain/offline-first-sync-and-conflict-resolution.spec.md` (`domain-architect`)
  - `.specs/02-data/local-sqlite-outbox-and-sync-audit-ledger.spec.md` (`data-architect`)
  - `.specs/03-integration/offline-sync-api-and-expo-app.spec.md` (`integration-architect`)
  - `.specs/04-security/security-audit-report.spec.md` (`security-auditor`)
- **Offline-First Clean Architecture Domain (`src/domain/`)**:
  - `FieldNote`, `GeoCoordinate` (validación WGS84 estricta), `LamportClock` thread-safe, `PendingMutation`, `ConflictRecord` y `ConflictResolver` (`KEEP_LOCAL`, `ACCEPT_REMOTE`, `MANUAL_MERGE`).
- **Local SQLite Outbox & Remote Sync Store (`src/infrastructure/persistence/` & `migrations/`)**:
  - `SQLiteDeviceStore`, `SQLiteRemoteSyncServerStore` (OCC por `base_version`), triggers *append-only* `BEFORE UPDATE`/`BEFORE DELETE` en `sync_audit_ledger` y `remote_sync_audit_ledger`, `seed_fieldnotes.py` (5 notas de inspección de campo) y `001_init_fieldnotes_sync.sql`.
- **Sync HTTP API, React Native (Expo 52 + TypeScript Strict) Blueprint & Mobile Simulator (`src/application/`, `src/interfaces/http/`, `mobile-app/`, `ui/`)**:
  - `FieldNotesApplicationService`, `server.py` (puerto `8097`, toggle offline/online, `503` offline gate, delta push/pull y resolución de conflictos), app React Native / Expo en `mobile-app/` con cero `any`, y simulador móvil en `ui/index.html`.
- **QA & Security Certification (`tests/`)**:
  - 45/45 pruebas unitarias, concurrencia de relojes de Lamport, pureza AST de dominio, higiene TypeScript, triggers append-only e integración HTTP certificadas por `qa-verifier` y `security-auditor` (`0 Critical, 0 High, 0 Medium`).

## [2026-11-18] - Adopción de Estructura Agéntica (Estándar GNV Manager)

### Added
- **`AGENTS.md`**, **`.agents/`**, **`.specs/00-infrastructure/`**, **`.env.*.example`** y **`docker-compose.dev.yml`**.
