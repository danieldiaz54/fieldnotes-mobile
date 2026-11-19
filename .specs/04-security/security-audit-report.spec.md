# SPEC-04: Security Certification & OWASP Top 10 Audit Report (`fieldnotes-mobile`)

- **Repository**: `fieldnotes-mobile` (Offline-First Mobile Field Inspection Sync Engine & Expo React Native Blueprint)
- **Audited Branch**: `feat/offline-first-sync-engine` (branched strictly from `dev`)
- **Audit Role**: `security-auditor` (DevSecOps & Application Security)
- **Governance Standard**: OWASP Top 10 (2021), OWASP MASVS / Offline-First Mobile Security, `.agents/security-auditor/persona.md`, `.agents/rules/gitflow-and-env-isolation.md`, `.agents/rules/ui-strict-types.md`
- **Aligned Specifications**:
  - `.specs/01-domain/offline-first-sync-and-conflict-resolution.spec.md`
  - `.specs/02-data/local-sqlite-outbox-and-sync-audit-ledger.spec.md`
  - `.specs/03-integration/offline-sync-api-and-expo-app.spec.md`
- **Audit Date**: `2026-11-19`
- **Certification Verdict**: **CERTIFIED — PASS** (0 Critical · 0 High · 0 Medium · 6 Low/Informational Production Hardening Advisories)

---

## 1. Executive Summary

An exhaustive static, architectural, and dynamic security audit was conducted across the full `fieldnotes-mobile` implementation on branch `feat/offline-first-sync-engine`, covering:

- Pure Domain Core (`src/domain/exceptions.py`, `src/domain/notes.py`, `src/domain/repositories.py`, `src/domain/sync_engine.py`)
- Application Orchestration (`src/application/services.py`)
- SQLite & PostgreSQL Persistence (`src/infrastructure/persistence/sqlite_sync_stores.py`, `src/infrastructure/persistence/seed_fieldnotes.py`, `migrations/001_init_fieldnotes_sync.sql`)
- HTTP/1.1 REST Boundary (`src/interfaces/http/server.py`, `src/main.py`)
- Interactive Web Simulator UI (`ui/index.html`)
- React Native / Expo 52 Strict TypeScript Blueprint (`mobile-app/tsconfig.json`, `mobile-app/package.json`, `mobile-app/app.json`, `mobile-app/src/**`)
- Containers, Secret Hygiene, CI & DevSecOps Governance (`Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `.gitignore`, `.env.*.example`, `.github/workflows/ci.yml`, `.agents/hooks.json`, `.agents/scripts/safety-gate.*`)

All mandatory security controls **PASSED** both static code review and live empirical verification. Zero Critical, High, or Medium vulnerabilities were detected. The branch is **certified** for integration into `dev` and eligible for promotion to `main` pending co-signature from `qa-verifier`.

### Audit Control Scorecard

| # | Security Control Domain | OWASP / MASVS Mapping | Status | Findings Summary |
| :--- | :--- | :--- | :--- | :--- |
| 1 | SQL Injection Prevention (`sqlite_sync_stores.py`, `SqliteOutboxRepository.ts`) | A03:2021 Injection | **PASS** | 33 Python SQLite calls + 15 Expo SQLite calls verified; **0** interpolated SQL queries; 100% positional `?` parameterization + live SQLi payload test passed. |
| 2 | Sync Audit Ledger Immutability (SQLite & PostgreSQL Triggers) | A08:2021 Software & Data Integrity Failures | **PASS** | `BEFORE UPDATE` & `BEFORE DELETE` triggers verified across local SQLite, remote SQLite, Expo SQLite, and PostgreSQL 16 (`APPEND_ONLY_LEDGER_VIOLATION`). |
| 3 | XSS & DOM Safety + Strict GPS Validation (`ui/index.html`) | A03:2021 XSS / MASVS-CODE | **PASS** | **0** unsafe sinks (`innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, inline `on*`); 100% `createElement`/`textContent`; strict WGS84 numeric guard verified (11/11 edge cases). |
| 4 | Strict TypeScript Hygiene (`mobile-app/`) | MASVS-CODE / UI Strict Types | **PASS** | `"strict": true`, `"noImplicitAny": true`, `"strictNullChecks": true`, `"noUncheckedIndexedAccess": true`; **0** `any`, `@ts-ignore`, or `@ts-expect-error` across `mobile-app/src/`. |
| 5 | HTTP Boundary Hardening & Error Sanitization (`server.py`) | A01, A04, A05, A09 | **PASS** | 64 KB ceiling (`413`), mandatory security headers on all responses, semantic codes (`400`/`404`/`405`/`409`/`413`/`422`/`503`/`500`), **0** stack-trace leakage. |
| 6 | Container Least Privilege (`Dockerfile`) | A05:2021 Security Misconfiguration | **PASS** | Multi-stage `python:3.12-slim` build, dedicated system user `fieldnotes`, `--chown=fieldnotes:fieldnotes`, `USER fieldnotes`, zero third-party runtime deps, active `HEALTHCHECK`. |
| 7 | Secret Protection, `.gitignore` & Env Isolation | A02:2021 Cryptographic Failures / Secret Hygiene | **PASS** | `.env.development`, `.env.production`, `/Docs/*`, `/docs/*` gitignored while preserving `!/docs/adr/**`; **0** secrets found across 77 scanned files; `?sslmode=require` in prod template. |
| 8 | Agentic Destructive Command Safety Gate (`.agents/hooks.json`) | DevSecOps Governance | **PASS** | `PreToolUse` hook active (`enabled: true`); empirically verified live interception + 10/10 unit checks blocking destructive DB/FS/git-force-main commands. |

---

## 2. Detailed Security Control Verification

### 2.1 SQL Injection Prevention & Append-Only Sync Audit Ledger Immutability

#### Python SQLite Stores (`src/infrastructure/persistence/sqlite_sync_stores.py`)
- **Static AST Audit**: Parsed `src/infrastructure/persistence/sqlite_sync_stores.py` via Python `ast` and inspected all **33** `execute` / `executescript` invocations:
  - **0** calls construct SQL using f-strings (`ast.JoinedStr`), `+` / `%` binary string operators (`ast.BinOp`), or `.format(...)`.
  - Every data-bearing DML/query call passes a static SQL string literal and a positional parameter tuple with `?` placeholders:
    - `SQLiteDeviceStore`: `_upsert_local_note_row` (L236–273), `upsert_local_note_offline` (L337–356), `list_pending_mutations` (L398–407), `mark_mutation_status` (L426–433), `record_conflict` (L444–479), `get_conflict` (L515–523), `mark_conflict_resolved` (L573–580), `append_sync_audit` (L600–618), `list_sync_audits` (L625–634), `get_note` (L665–674), `enqueue_mutation` (L707–726).
    - `SQLiteRemoteSyncServerStore`: `_upsert_remote_note_row` (L827–864), `push_mutation` (L919–928), `force_remote_edit` (L1001–1010), `pull_delta_since` (L1035–1045), `append_remote_sync_audit` (L1062–1080), `list_remote_sync_audits` (L1087–1096), `get_note` (L1121–1130), `list_notes` (L1143–1165).
  - Test helper `execute_raw_mutation_for_test` (L637–650, L1099–1112) is not referenced by `FieldNotesApplicationService` or `FieldNotesHTTPRequestHandler`, still passes `params` via parameterized binding, and maps `APPEND_ONLY_LEDGER_VIOLATION` aborts to `AppendOnlyLedgerError`.
- **Database Schema Integrity Constraints**: All tables enforce SQLite `CHECK` constraints for enumerations (`severity`, `sync_state`, `operation`, `status`, `strategy`), WGS84 bounds (`latitude >= -90.0 AND latitude <= 90.0`, `longitude >= -180.0 AND longitude <= 180.0`), `version >= 1`, `lamport_ts >= 1`, `deleted IN (0, 1)`, and non-negative audit metrics, with `PRAGMA foreign_keys = ON`.
- **Dynamic SQL Injection Resistance Test**: Inserted and retrieved a `FieldNote` containing `"NOTE-SQLI-1'; DROP TABLE local_notes; --"` as `note_id` and `"' OR '1'='1' -- <script>alert(1)</script>"` as `title`. The values were stored literally without executing injected statements; `local_notes` and `sync_audit_ledger` remained completely intact.

#### Mobile Expo SQLite Adapter (`mobile-app/src/data/SqliteOutboxRepository.ts`)
- **Parameterization Verification**: Inspected all **15** `expo-sqlite` database query calls (`runAsync`, `getAllAsync`, `getFirstAsync`) across L206–489:
  - **0** template-literal `${...}` interpolations inside SQL strings; every dynamic argument is passed as positional `?` parameters (L242–289, L317–321, L331–362, L369–394, L437–459, L465–478).
  - Local outbox mutations and conflict updates execute inside `db.withTransactionAsync(...)` (L241–290, L368–395, L436–460), preventing partial-write corruption.
  - All deserialized rows and conflict JSON blobs pass through domain validator `validateFieldNote(...)` (L158, L222, L326, L419–420, L435).
  - HTTP path parameters are encoded with `encodeURIComponent(conflictId)` (L532).

#### Append-Only Sync Audit Ledger Triggers (SQLite & PostgreSQL 16)
Every persistence engine enforces database-level immutability on the sync audit ledger:

| Persistence Engine / Artifact | Table | Trigger Name | Event | Enforcement Mechanism | Verification |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Python Local SQLite (`sqlite_sync_stores.py` L219–229) | `sync_audit_ledger` | `trg_sync_audit_ledger_no_update` / `_no_delete` | `BEFORE UPDATE` / `BEFORE DELETE` | `RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: ...')` | **Verified Live** (`AppendOnlyLedgerError` raised on both `UPDATE` & `DELETE`) |
| Python Remote SQLite (`sqlite_sync_stores.py` L810–820) | `remote_sync_audit_ledger` | `trg_remote_sync_audit_ledger_no_update` / `_no_delete` | `BEFORE UPDATE` / `BEFORE DELETE` | `RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: ...')` | **Verified Live** (`AppendOnlyLedgerError` raised on both `UPDATE` & `DELETE`) |
| Expo Mobile SQLite (`SqliteOutboxRepository.ts` L144–154) | `sync_audit_ledger` | `trg_sync_audit_ledger_no_update` / `_no_delete` | `BEFORE UPDATE` / `BEFORE DELETE` | `RAISE(ABORT, 'APPEND_ONLY_LEDGER_VIOLATION: ...')` | **Verified Statically** in `SCHEMA_SQL` |
| PostgreSQL 16 Migration (`migrations/001_init_fieldnotes_sync.sql` L61–98) | `sync_audit_ledger` | `trg_sync_audit_ledger_no_update` / `_no_delete` | `BEFORE UPDATE` / `BEFORE DELETE` | PL/pgSQL `prevent_sync_audit_mutation()` raising `APPEND_ONLY_LEDGER_VIOLATION` (`ERRCODE = 'check_violation'`) | **Verified Statically** (idempotent `pg_trigger` check inside `BEGIN ... COMMIT`) |

---

### 2.2 XSS & DOM Safety + Strict Numeric GPS Validation (`ui/index.html`)

#### Static Sink Audit (4,628 Lines Scanned)
Automated regex and manual inspection of `ui/index.html` confirmed **zero** unsafe HTML parsing or dynamic code execution sinks:

| Unsafe Sink / Pattern | Matches Found | Status |
| :--- | :---: | :--- |
| `.innerHTML` | 0 | **PASS** |
| `.outerHTML` | 0 | **PASS** |
| `.insertAdjacentHTML` | 0 | **PASS** |
| `document.write` / `document.writeln` | 0 | **PASS** |
| `eval(...)` / `new Function(...)` | 0 | **PASS** |
| `javascript:` URI scheme | 0 | **PASS** |
| `srcdoc` attribute | 0 | **PASS** |
| Inline HTML `on*=` attributes | 0 | **PASS** |
| `DOMParser` / `createContextualFragment` | 0 | **PASS** |

- **Pure Safe DOM Construction**: Every dynamic note card, conflict diff pane, outbox row, remote table cell, audit ledger entry, banner, and telemetry pill is constructed via `el(tag, attrs, children)` (L2304–2352) and `svgEl(tag, attrs, children)` (L2354–2369) using `document.createElement`, `document.createElementNS`, `document.createTextNode`, `.textContent`, `addEventListener`, and `.replaceChildren(...)`.
- **URL Encoding**: Dynamic path segments use `encodeURIComponent(...)` (`cmdSimulateRemoteEdit` L3386, `cmdResolveConflict` L3481).

#### Strict Numeric GPS Validation
- `STRICT_NUMBER_RE = /^[+-]?(?:\d+\.?\d*|\.\d+)$/` (L2152) paired with `parseStrictCoordinate(rawText, minVal, maxVal, label)` (L2251–2271) enforces strict finite decimal validation before submission (`validateEditorCoordinates` L3719–3741, form submit guard L4540–4545) and inside the standalone local replica (`upsertLocalNote` L2646–2663).
- **Verified Test Matrix (11/11 passed)**: Accepts valid WGS84 decimals (`4.7110`, `-74.0721`, `90`, `-180.0`); deterministically rejects `""`, `"NaN"`, `"Infinity"`, `"1e5"`, `"4.71abc"`, `"true"`, `"<script>alert(1)</script>"`, and values outside `latitude ∈ [-90, 90]` or `longitude ∈ [-180, 180]`.
- Backend `GeoCoordinate` (`src/domain/notes.py` L55–75, L108–129) independently enforces non-`bool`, `math.isfinite`, and range checks so client-side bypass attempts are rejected with `400 Bad Request` (`ERR-400-INVALID-FIELD-NOTE`).

---

### 2.3 Strict TypeScript Hygiene (`mobile-app/`)

- **Compiler Configuration (`mobile-app/tsconfig.json` L3–10)**:
  - `"strict": true`
  - `"noImplicitAny": true`
  - `"strictNullChecks": true`
  - `"noUncheckedIndexedAccess": true`
  - `"noFallthroughCasesInSwitch": true`
  - `"forceConsistentCasingInFileNames": true`
- **Source Hygiene Scan (`mobile-app/src/`)**: Audited all 4 TypeScript/TSX modules (`FieldNote.ts`, `ConflictResolver.ts`, `SqliteOutboxRepository.ts`, `FieldNotesScreen.tsx`):
  - **0** occurrences of `: any`, `as any`, `<any>`, `Array<any>`, `Record<..., any>`, `@ts-ignore`, `@ts-expect-error`, `eval`, or `dangerouslySetInnerHTML`.
  - All exception handlers type caught values as `error: unknown` with `instanceof` type narrowing (`FieldNotesScreen.tsx` L145, L213).
  - All domain contracts use `readonly` fields and `Object.freeze(...)` (`FieldNote.ts` L161, L197; `SqliteOutboxRepository.ts` L229, L312, L416).
  - Continuous guard enforced in `.github/workflows/ci.yml` (L96–109).

---

### 2.4 HTTP Boundary Hardening & Error Sanitization (`src/interfaces/http/server.py`)

Live verification against an ephemeral `FieldNotesThreadingHTTPServer` (`127.0.0.1:0`) confirmed all transport-layer and input-boundary defenses:

| Control | Implementation Reference | Live Empirical Test Result | Status |
| :--- | :--- | :--- | :--- |
| **64 KB Payload Ceiling (`413`)** | `MAX_JSON_BODY_BYTES = 64 * 1024` (L23); pre-read `Content-Length` check (L365–369) + post-read length check (L376–380) | 65,558-byte payload rejected with `413` (`ERR-413-PAYLOAD-TOO-LARGE`); exact 65,536-byte valid JSON accepted (`201 Created`) | **PASS** |
| **Chunked / Missing Length Fail-Closed** | `_read_json_body` (L351–355): rejects missing `Content-Length` with `400` (`allow_empty=False`) or returns `{}` without reading socket (`allow_empty=True`) | Prevents unbounded chunked stream exhaustion DoS | **PASS** |
| **Mandatory Security Headers** | `_send_security_and_cors_headers` (L285–288) called by `_send_json`, `_send_html`, and `do_OPTIONS` | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` verified on `200` (HTML & JSON), `201`, `204`, `400`, `404`, `405`, `413`, `422`, `503` | **PASS** |
| **Strict Input & Path Sanitization** | `_validate_path_identifier` (L73–77), `_parse_bool_query_param` (L80–90), `_parse_limit_query_param` (L93–107), `set_connectivity` strict `bool` check (`services.py` L179) | Malformed JSON, non-bool `is_online: "false"`, GPS `latitude: 91.5`, null-byte path `NOTE%00BAD`, and `?limit=9999` all returned `400` | **PASS** |
| **Semantic Status Mapping (`400`/`404`/`405`/`409`/`422`/`503`)** | `map_exception_to_http` (L110–239), `_send_not_found` (L324–335), `_send_method_not_allowed` (L337–341) | Verified `404` on unknown route & unknown `note_id`, `405` + `Allow` header on `DELETE` and `GET /api/v1/sync/run`, `409` for `SyncConflictError`, `422` for blank `MANUAL_MERGE`, `503` for offline sync | **PASS** |
| **Zero Stack-Trace Leakage** | Fallback `500` branch in `map_exception_to_http` (L229–239) | Forced `RuntimeError("SECRET_SQLITE_PATH=...")` mapped to static `"An unexpected internal server error occurred."` with `details: {}` and zero traceback/path leakage | **PASS** |

---

### 2.5 Container Least Privilege, Secret Hygiene & Safety Gate Governance

#### Container Security (`Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`)
- **Multi-Stage Minimal Runtime (`Dockerfile`)**: Uses `python:3.12-slim AS builder` (L2) and `python:3.12-slim AS runtime` (L15) with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`.
- **Non-Root Principle of Least Privilege**:
  - Creates dedicated system group and user `fieldnotes` (`groupadd --system fieldnotes && useradd --system --gid fieldnotes --create-home --home-dir /app fieldnotes`, L25–26).
  - Sets ownership on `/app` and `/app/.data` and copies only required runtime artifacts (`pyproject.toml`, `requirements.txt`, `src`, `ui`, `migrations`) via `COPY --from=builder --chown=fieldnotes:fieldnotes` (L31–34).
  - Switches to `USER fieldnotes` (L36) prior to `EXPOSE 8097`, `HEALTHCHECK` (L40–41), and `CMD ["python", "-m", "src.main"]` (L43).
- **Minimal Supply-Chain Surface (OWASP A06)**: Core Python engine has **zero** external runtime dependencies (`pyproject.toml` `dependencies = []`), eliminating transitive package vulnerability exposure.
- **CI Token Least Privilege (`.github/workflows/ci.yml` L9–10)**: Explicitly sets `permissions: contents: read`.

#### Secret Protection & Environment Isolation (`.gitignore`, `.env.*.example`)
- **Empirical `git check-ignore -v` Verification**:
  - `.env.development` → ignored by `.gitignore:9` (**PASS**)
  - `.env.production` → ignored by `.gitignore:10` (**PASS**)
  - `Docs/secret.pdf` & `docs/README.local.md` → ignored by `.gitignore:3–4` (`/Docs/*`, `/docs/*`) (**PASS**)
  - `docs/adr/001-local-first-sqlite-outbox-sync.md` → preserved by `.gitignore:5–6` (`!/docs/adr/`, `!/docs/adr/**`) (**PASS**)
  - `.agents/.agents/scripts/safety-gate.py` → ignored by `.gitignore:13` (**PASS**)
- **Repository Secret Scan**: Scanned all **77** files across the workspace (excluding `.git`, caches); **0** AWS keys, GitHub PATs, private keys, Slack tokens, or production credentials detected. No real `.env`, `.env.development`, or `.env.production` files exist on disk or in Git index.
- **Dev vs Prod Isolation**:
  - `.env.development.example` and `docker-compose.dev.yml` target isolated local dev storage (`./.data/fieldnotes_device_dev.sqlite3`, `localhost:5442/fieldnotes_sync_dev`).
  - `.env.production.example` uses placeholder tokens (`__PROD_POSTGRES_USER__`, `__PROD_POSTGRES_PASSWORD__`, `__PROD_POSTGRES_HOST__`) and mandates encrypted PostgreSQL transport (`?sslmode=require`).

#### Destructive Command Safety Gate (`.agents/hooks.json`, `.agents/scripts/safety-gate.py` & `.js`)
- `.agents/hooks.json` configures `"safety-gate": { "enabled": true }` on `PreToolUse` for `run_command` executing `python scripts/safety-gate.py` (with parity Node.js fallback `safety-gate.js`).
- **Live & Unit Verification**:
  - Confirmed live `PreToolUse` enforcement actively intercepts commands matching destructive patterns.
  - Unit verification of `safety-gate.py` (`evaluate_command`) passed **10/10** test cases:
    - **Denied (`"decision": "deny"`)**: `DROP DATABASE`, `DROP SCHEMA`, `TRUNCATE TABLE`, `rm -rf /`, `rmdir /s /q c:\temp`, `format c:`, `git push --force origin main`, `git push -f origin main`.
    - **Allowed (`"decision": "allow"`)**: `python -m unittest discover -s tests -v`, `git status --short`.

---

## 3. Hardening Advisories (Low / Informational — Non-Blocking)

The following items represent defense-in-depth hardening recommendations prior to multi-tenant public internet deployment. None represent exploitable vulnerabilities in the current local-first reference architecture or block merge to `dev` / `main`:

| ID | Severity | Location | Observation & Recommended Production Hardening |
| :--- | :--- | :--- | :--- |
| **ADV-01** | **Low** | `.gitignore` | `.env.development`, `.env.production`, and `/Docs/*` / `/docs/*` are properly ignored, but generic `.env`, `.env.local`, `.data/`, `*.sqlite3`, `__pycache__/`, `*.py[cod]`, and `mobile-app/node_modules/` are not yet listed. Add these patterns before future commits so local runtime DBs or bytecode caches cannot be accidentally staged. |
| **ADV-02** | **Low** | `src/interfaces/http/server.py` (L289, L418–567) | `Access-Control-Allow-Origin: *` and unauthenticated `/api/v1/*` routes fit the local portfolio workbench. For internet-exposed multi-tenant deployments, restrict CORS to an explicit origin allowlist and add short-lived JWT / mTLS device-attestation authentication with per-device RBAC (OWASP A01). |
| **ADV-03** | **Low** | `src/interfaces/http/server.py` (L285–291, L313–322) | Add a strict `Content-Security-Policy` header on HTML responses (externalizing or nonce-gating `ui/index.html` inline script/style while permitting Google Fonts) and override `FieldNotesHTTPRequestHandler.version_string()` to suppress the default `Server: BaseHTTP/0.6 Python/3.12.x` version banner (OWASP A05). |
| **ADV-04** | **Low** | `mobile-app/app.json` (L35), `mobile-app/src/data/SqliteOutboxRepository.ts` (L498) | Default `syncApiBaseUrl` uses plaintext `http://127.0.0.1:8097` for local simulator testing. Production Expo builds must enforce HTTPS (`https://`), disable cleartext traffic via iOS ATS / Android Network Security Config, and consider TLS certificate pinning (MASVS-NETWORK). |
| **ADV-05** | **Informational** | `docker-compose.dev.yml`, `docker-compose.yml` | Production `Dockerfile` enforces `USER fieldnotes`, whereas `docker-compose.dev.yml` runs raw `python:3.12-slim` as default root for bind-mount hot reload, and `docker-compose.yml` includes local dev fallback DB passwords. Production orchestrators (K8s/ECS) should deploy the `Dockerfile` image with secret-manager injection, `allowPrivilegeEscalation: false`, and dropped Linux capabilities. |
| **ADV-06** | **Informational** | `mobile-app/src/data/SqliteOutboxRepository.ts` (L177–180) | `generateLocalId` uses `Date.now()` + `Math.random()` for non-security outbox mutation IDs (`mut-*`). Replace with `expo-crypto` `randomUUID()` in production mobile builds to match Python's `uuid.uuid4()` entropy under burst offline captures. |

---

## 4. Security Certification Sign-Off

```text
CERTIFICATION DECISION : CERTIFIED — PASS
REPOSITORY             : fieldnotes-mobile
BRANCH                 : feat/offline-first-sync-engine
CRITICAL / HIGH / MED  : 0 / 0 / 0
LOW / INFORMATIONAL    : 4 / 2 (Non-blocking production hardening advisories)
AUDITED BY             : security-auditor (DevSecOps & Application Security)
PROMOTION GATE         : Approved for merge into `dev`; approved for `dev` -> `main`
                         release upon co-certification by `qa-verifier`.
```
