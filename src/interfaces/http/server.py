"""Zero-dependency HTTP/1.1 REST Server built on Python 3.12 standard library."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from src.application.services import FieldNotesApplicationService
from src.domain.exceptions import (
    DomainError,
    InvalidConflictResolutionError,
    InvalidFieldNoteError,
    NoteNotFoundError,
    OfflineNetworkError,
    SyncConflictError,
)

MAX_JSON_BODY_BYTES = 64 * 1024  # 64 KB strict payload ceiling
REPO_ROOT = Path(__file__).resolve().parents[3]
UI_INDEX_PATH = REPO_ROOT / "ui" / "index.html"

_SIMULATE_EDIT_RE = re.compile(r"^/api/v1/notes/remote/([^/]+)/simulate-edit$")
_RESOLVE_CONFLICT_RE = re.compile(r"^/api/v1/conflicts/([^/]+)/resolve$")

_STATIC_ROUTE_METHODS: dict[str, tuple[str, ...]] = {
    "/": ("GET",),
    "/ui": ("GET",),
    "/index.html": ("GET",),
    "/api/v1/health": ("GET",),
    "/api/v1/device/connectivity": ("POST",),
    "/api/v1/notes/local": ("GET", "POST"),
    "/api/v1/notes/remote": ("GET",),
    "/api/v1/sync/queue": ("GET",),
    "/api/v1/sync/run": ("POST",),
    "/api/v1/conflicts": ("GET",),
    "/api/v1/sync/audit": ("GET",),
}


class PayloadTooLargeError(ValueError):
    """Raised when an HTTP request body exceeds MAX_JSON_BODY_BYTES (64 KB)."""


class MalformedJsonError(ValueError):
    """Raised when an HTTP request body is not valid UTF-8 JSON or not a JSON object."""


class MethodNotAllowedError(ValueError):
    """Raised when an HTTP method is not permitted for a matched route."""

    def __init__(self, method: str, path: str, allowed: tuple[str, ...]) -> None:
        self.method = method
        self.path = path
        self.allowed = allowed
        super().__init__(
            f"Method '{method}' is not allowed for '{path}'. Allowed: {', '.join(allowed)}."
        )


def _allowed_methods_for_path(path: str) -> tuple[str, ...] | None:
    if path in _STATIC_ROUTE_METHODS:
        return _STATIC_ROUTE_METHODS[path]
    if _SIMULATE_EDIT_RE.match(path) or _RESOLVE_CONFLICT_RE.match(path):
        return ("POST",)
    return None


def _validate_path_identifier(raw_segment: str, field_name: str) -> str:
    decoded = unquote(raw_segment)
    if not decoded.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise InvalidFieldNoteError(f"Invalid path parameter '{field_name}'.")
    return decoded


def _parse_bool_query_param(qs: dict[str, list[str]], name: str, default: bool = False) -> bool:
    if name not in qs:
        return default
    raw = qs[name][0].strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise InvalidFieldNoteError(
        f"Query parameter '{name}' must be a boolean ('true' or 'false'); got {qs[name][0]!r}."
    )


def _parse_limit_query_param(qs: dict[str, list[str]], default: int = 50) -> int:
    if "limit" not in qs:
        return default
    raw = qs["limit"][0].strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise InvalidFieldNoteError(
            f"Query parameter 'limit' must be a positive integer; got {raw!r}."
        ) from exc
    if parsed < 1 or parsed > 500:
        raise InvalidFieldNoteError(
            f"Query parameter 'limit' must be between 1 and 500; got {parsed}."
        )
    return parsed


def map_exception_to_http(exc: Exception) -> tuple[int, dict[str, Any]]:
    """Map domain and protocol exceptions to semantic HTTP status codes with zero stack traces."""
    if isinstance(exc, PayloadTooLargeError):
        return (
            413,
            {
                "error": {
                    "code": "ERR-413-PAYLOAD-TOO-LARGE",
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "details": {"max_bytes": MAX_JSON_BODY_BYTES},
                }
            },
        )

    if isinstance(exc, MalformedJsonError):
        return (
            400,
            {
                "error": {
                    "code": "ERR-400-MALFORMED-JSON",
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "details": {},
                }
            },
        )

    if isinstance(exc, InvalidFieldNoteError):
        return (
            400,
            {
                "error": {
                    "code": exc.code,
                    "type": exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    if isinstance(exc, NoteNotFoundError):
        return (
            404,
            {
                "error": {
                    "code": exc.code,
                    "type": exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    if isinstance(exc, MethodNotAllowedError):
        return (
            405,
            {
                "error": {
                    "code": "ERR-405-METHOD-NOT-ALLOWED",
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "details": {"allowed_methods": list(exc.allowed)},
                }
            },
        )

    if isinstance(exc, SyncConflictError):
        return (
            409,
            {
                "error": {
                    "code": exc.code,
                    "type": exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    if isinstance(exc, InvalidConflictResolutionError):
        return (
            422,
            {
                "error": {
                    "code": exc.code,
                    "type": exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    if isinstance(exc, OfflineNetworkError):
        return (
            503,
            {
                "error": {
                    "code": exc.code,
                    "type": exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    if isinstance(exc, DomainError):
        return (
            422,
            {
                "error": {
                    "code": exc.code,
                    "type": exc.__class__.__name__,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    return (
        500,
        {
            "error": {
                "code": "ERR-500-INTERNAL-SERVER-ERROR",
                "type": "InternalServerError",
                "message": "An unexpected internal server error occurred.",
                "details": {},
            }
        },
    )


class FieldNotesThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer bound to a FieldNotesApplicationService instance."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        service: FieldNotesApplicationService,
        *,
        owns_service: bool = False,
    ) -> None:
        self.service = service
        self._owns_service = owns_service
        super().__init__(server_address, RequestHandlerClass)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self._owns_service:
                self.service.close()


class FieldNotesHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 REST request handler for FieldNotes Mobile Offline-First Sync API v1."""

    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> FieldNotesApplicationService:
        server = self.server
        if isinstance(server, FieldNotesThreadingHTTPServer):
            return server.service
        raise RuntimeError("Server is not configured with FieldNotesApplicationService.")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress noisy stderr logs during automated test runs unless enabled."""
        if os.environ.get("FIELDNOTES_ACCESS_LOG", "0") == "1":
            super().log_message(format, *args)

    def _send_security_and_cors_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, X-Request-ID")

    def _send_json(
        self,
        status_code: int,
        payload: dict[str, Any] | list[Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_security_and_cors_headers()
        if extra_headers:
            for header_name, header_val in extra_headers.items():
                self.send_header(header_name, header_val)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    def _send_html(self, status_code: int, html_content: str) -> None:
        raw = html_content.encode("utf-8")
        self.send_response(status_code)
        self._send_security_and_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    def _send_not_found(self, path: str) -> None:
        self._send_json(
            404,
            {
                "error": {
                    "code": "ERR-404-ROUTE-NOT-FOUND",
                    "type": "NotFoundError",
                    "message": f"Route '{path}' was not found.",
                    "details": {"path": path},
                }
            },
        )

    def _send_method_not_allowed(self, method: str, path: str, allowed: tuple[str, ...]) -> None:
        exc = MethodNotAllowedError(method=method, path=path, allowed=allowed)
        status_code, body = map_exception_to_http(exc)
        allow_header = ", ".join((*allowed, "OPTIONS"))
        self._send_json(status_code, body, extra_headers={"Allow": allow_header})

    def _handle_exception(self, exc: Exception) -> None:
        status_code, body = map_exception_to_http(exc)
        extra_headers: dict[str, str] | None = None
        if isinstance(exc, MethodNotAllowedError):
            extra_headers = {"Allow": ", ".join((*exc.allowed, "OPTIONS"))}
        self._send_json(status_code, body, extra_headers=extra_headers)

    def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any]:
        content_length_raw = self.headers.get("Content-Length")
        if content_length_raw is None:
            if allow_empty:
                return {}
            raise MalformedJsonError("Missing Content-Length header or JSON request body.")

        try:
            content_length = int(content_length_raw)
        except ValueError as exc:
            raise MalformedJsonError("Invalid Content-Length header.") from exc

        if content_length < 0:
            raise MalformedJsonError("Negative Content-Length header is invalid.")

        if content_length > MAX_JSON_BODY_BYTES:
            raise PayloadTooLargeError(
                f"Request body of {content_length} bytes exceeds maximum ceiling of "
                f"{MAX_JSON_BODY_BYTES} bytes (64 KB)."
            )

        if content_length == 0:
            if allow_empty:
                return {}
            raise MalformedJsonError("Request body cannot be empty.")

        raw_bytes = self.rfile.read(content_length)
        if len(raw_bytes) > MAX_JSON_BODY_BYTES:
            raise PayloadTooLargeError(
                f"Request body exceeds maximum ceiling of {MAX_JSON_BODY_BYTES} bytes (64 KB)."
            )

        try:
            decoded = raw_bytes.decode("utf-8")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MalformedJsonError("Request body is not valid UTF-8 JSON.") from exc

        if not isinstance(parsed, dict):
            raise MalformedJsonError("JSON request payload must be an object.")

        return parsed

    def _reject_unsupported_method(self, method: str) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/") or "/"
        allowed = _allowed_methods_for_path(path)
        if allowed is not None:
            self._send_method_not_allowed(method, path, allowed)
            return
        self._send_method_not_allowed(method, path, ("GET", "POST"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_security_and_cors_headers()
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_unsupported_method("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_unsupported_method("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_unsupported_method("PATCH")

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/") or "/"
        qs = parse_qs(parsed_url.query, keep_blank_values=True)

        try:
            if path in ("/", "/ui", "/index.html"):
                if UI_INDEX_PATH.exists():
                    html_text = UI_INDEX_PATH.read_text(encoding="utf-8")
                else:
                    html_text = (
                        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                        "<title>FieldNotes Mobile — Offline-First Sync Workbench</title></head>"
                        "<body><h1>FieldNotes Mobile Offline-First Sync Engine</h1>"
                        "<p>REST API active at <code>/api/v1/health</code>.</p></body></html>"
                    )
                self._send_html(200, html_text)
                return

            if path == "/api/v1/health":
                self._send_json(200, self.service.get_health())
                return

            if path == "/api/v1/notes/local":
                notes = self.service.list_local_notes()
                self._send_json(
                    200,
                    {
                        "notes": notes,
                        "total": len(notes),
                        "is_online": self.service.is_online,
                        "lamport_ts": self.service.clock.current(),
                    },
                )
                return

            if path == "/api/v1/notes/remote":
                notes = self.service.list_remote_notes()
                self._send_json(
                    200,
                    {
                        "notes": notes,
                        "total": len(notes),
                    },
                )
                return

            if path == "/api/v1/sync/queue":
                only_queued = _parse_bool_query_param(qs, "only_queued", default=False)
                mutations = self.service.list_pending_mutations(only_queued=only_queued)
                queued_count = sum(1 for item in mutations if item["status"] == "QUEUED")
                self._send_json(
                    200,
                    {
                        "mutations": mutations,
                        "queue": mutations,
                        "total": len(mutations),
                        "queued_count": queued_count,
                    },
                )
                return

            if path == "/api/v1/conflicts":
                unresolved_only = _parse_bool_query_param(qs, "unresolved_only", default=False)
                conflicts = self.service.list_conflicts(unresolved_only=unresolved_only)
                unresolved_count = sum(1 for item in conflicts if not item["resolved"])
                self._send_json(
                    200,
                    {
                        "conflicts": conflicts,
                        "total": len(conflicts),
                        "unresolved_count": unresolved_count,
                    },
                )
                return

            if path == "/api/v1/sync/audit":
                limit = _parse_limit_query_param(qs, default=50)
                audits = self.service.list_sync_audits(limit=limit)
                self._send_json(
                    200,
                    {
                        "audits": audits,
                        "ledger": audits,
                        "total": len(audits),
                    },
                )
                return

            allowed = _allowed_methods_for_path(path)
            if allowed is not None:
                self._send_method_not_allowed("GET", path, allowed)
                return

            self._send_not_found(path)
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/") or "/"

        try:
            if path == "/api/v1/device/connectivity":
                payload = self._read_json_body(allow_empty=False)
                if "is_online" not in payload:
                    raise InvalidFieldNoteError(
                        "Request body must include strict boolean field 'is_online'."
                    )
                result = self.service.set_connectivity(payload["is_online"])
                self._send_json(200, result)
                return

            if path == "/api/v1/notes/local":
                payload = self._read_json_body(allow_empty=False)
                result = self.service.upsert_local_note(payload)
                status_code = 201 if result["mutation"]["base_version"] == 0 else 200
                self._send_json(status_code, result)
                return

            if path == "/api/v1/sync/run":
                self._read_json_body(allow_empty=True)
                result = self.service.run_delta_sync()
                self._send_json(200, result)
                return

            sim_match = _SIMULATE_EDIT_RE.match(path)
            if sim_match:
                note_id = _validate_path_identifier(sim_match.group(1), "note_id")
                payload = self._read_json_body(allow_empty=False)
                result = self.service.simulate_remote_edit(note_id, payload)
                self._send_json(200, result)
                return

            resolve_match = _RESOLVE_CONFLICT_RE.match(path)
            if resolve_match:
                conflict_id = _validate_path_identifier(resolve_match.group(1), "conflict_id")
                payload = self._read_json_body(allow_empty=False)
                result = self.service.resolve_conflict(conflict_id, payload)
                self._send_json(200, result)
                return

            allowed = _allowed_methods_for_path(path)
            if allowed is not None:
                self._send_method_not_allowed("POST", path, allowed)
                return

            self._send_not_found(path)
        except Exception as exc:
            self._handle_exception(exc)


def create_http_server(
    host: str = "127.0.0.1",
    port: int = 8097,
    service: FieldNotesApplicationService | None = None,
) -> ThreadingHTTPServer:
    """Create and configure a ThreadingHTTPServer bound to `(host, port)`."""
    owns_service = False
    if service is None:
        device_db_path = os.environ.get("LOCAL_SQLITE_PATH", ":memory:") or ":memory:"
        remote_db_path = (
            ":memory:"
            if device_db_path == ":memory:"
            else (os.environ.get("REMOTE_SQLITE_PATH", "") or ":memory:")
        )
        service = FieldNotesApplicationService(
            device_db_path=device_db_path,
            remote_db_path=remote_db_path,
            auto_seed=True,
        )
        owns_service = True

    return FieldNotesThreadingHTTPServer(
        (host, port),
        FieldNotesHTTPRequestHandler,
        service=service,
        owns_service=owns_service,
    )


def main() -> None:
    """Start the FieldNotes Mobile Offline-First Sync HTTP Server."""
    host = os.environ.get("FIELDNOTES_HOST", "0.0.0.0")
    port = int(os.environ.get("FIELDNOTES_PORT", "8097"))
    device_db_path = (
        os.environ.get("LOCAL_SQLITE_PATH", "./.data/fieldnotes_device_dev.sqlite3")
        or "./.data/fieldnotes_device_dev.sqlite3"
    )
    if device_db_path == ":memory:":
        remote_db_path = ":memory:"
    else:
        remote_db_path = (
            os.environ.get("REMOTE_SQLITE_PATH", "./.data/fieldnotes_remote_dev.sqlite3")
            or "./.data/fieldnotes_remote_dev.sqlite3"
        )
    service = FieldNotesApplicationService(
        device_db_path=device_db_path,
        remote_db_path=remote_db_path,
        auto_seed=True,
    )
    server = create_http_server(host=host, port=port, service=service)
    bound_host, bound_port = server.server_address[:2]
    print(f"FieldNotes Mobile Offline-First Sync Server listening on http://{bound_host}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
