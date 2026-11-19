"""End-to-end HTTP/1.1 integration tests for FieldNotes Mobile Offline-First Sync API."""

from __future__ import annotations

import http.client
import json
import os
import threading
from typing import Any
import unittest
from unittest.mock import patch

from src.domain.exceptions import SyncConflictError
from src.interfaces.http.server import (
    MAX_JSON_BODY_BYTES,
    create_http_server,
    map_exception_to_http,
)

FORBIDDEN_LEAK_MARKERS = (
    "Traceback (most recent call last)",
    'File "',
    '.py", line',
    "sqlite3.",
    "OperationalError",
)


class _LiveServerTestCase(unittest.TestCase):
    """Base test case booting an isolated in-memory HTTP server in a daemon thread."""

    def setUp(self) -> None:
        with patch.dict(os.environ, {"LOCAL_SQLITE_PATH": ":memory:"}, clear=False):
            self.server = create_http_server("127.0.0.1", 0)
        self.host, self.port = self.server.server_address[:2]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3.0)

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5.0)
        req_headers = dict(headers or {})
        if body is not None and "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            conn.request(method, path, body=body, headers=req_headers)
            response = conn.getresponse()
            raw_text = response.read().decode("utf-8", errors="replace")
            resp_headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, resp_headers, raw_text
        finally:
            conn.close()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any], str]:
        raw_body = (
            json.dumps(payload).encode("utf-8") if payload is not None else None
        )
        status, headers, raw_text = self._request_raw(method, path, body=raw_body)
        parsed = json.loads(raw_text) if raw_text else {}
        return status, headers, parsed, raw_text

    def _assert_security_headers(self, headers: dict[str, str]) -> None:
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")

    def _assert_no_stack_trace(self, raw_text: str) -> None:
        for marker in FORBIDDEN_LEAK_MARKERS:
            self.assertNotIn(
                marker,
                raw_text,
                f"Internal leak marker {marker!r} exposed in HTTP response: {raw_text}",
            )


class TestHttpOfflineSyncEndToEnd(_LiveServerTestCase):
    """End-to-end verification of HTML workbench, health, offline queue, 503 gate, and merge."""

    def test_workbench_html_health_and_seeded_notes(self) -> None:
        status, headers, html = self._request_raw("GET", "/")
        self.assertEqual(status, 200)
        self._assert_security_headers(headers)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn("FieldNotes Mobile", html)

        ui_status, ui_headers, _ = self._request_raw("GET", "/ui")
        self.assertEqual(ui_status, 200)
        self._assert_security_headers(ui_headers)

        opt_status, opt_headers, _ = self._request_raw("OPTIONS", "/api/v1/notes/local")
        self.assertEqual(opt_status, 204)
        self.assertEqual(opt_headers.get("access-control-allow-origin"), "*")

        h_status, h_headers, health, _ = self._request_json("GET", "/api/v1/health")
        self.assertEqual(h_status, 200)
        self._assert_security_headers(h_headers)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["service"], "fieldnotes-mobile")
        self.assertTrue(health["is_online"])
        self.assertEqual(health["lamport_ts"], 5)
        self.assertEqual(health["local_notes_count"], 5)
        self.assertEqual(health["remote_notes_count"], 5)
        self.assertEqual(health["queued_mutations_count"], 0)
        self.assertEqual(health["unresolved_conflicts_count"], 0)

        n_status, n_headers, local_body, _ = self._request_json("GET", "/api/v1/notes/local")
        self.assertEqual(n_status, 200)
        self._assert_security_headers(n_headers)
        self.assertEqual(local_body["total"], 5)
        self.assertEqual(len(local_body["notes"]), 5)
        self.assertEqual(
            [note["note_id"] for note in local_body["notes"]],
            [f"NOTE-SITE-00{i}" for i in range(1, 6)],
        )
        self.assertTrue(all(note["sync_state"] == "SYNCED" for note in local_body["notes"]))

    def test_full_offline_first_capture_conflict_detection_and_manual_merge_flow(self) -> None:
        # 1. Switch offline (and reject invalid non-bool connectivity inputs with 400).
        for bad_online in ("false", 0, 1, None):
            b_status, b_headers, b_body, b_raw = self._request_json(
                "POST",
                "/api/v1/device/connectivity",
                {"is_online": bad_online},
            )
            self.assertEqual(b_status, 400)
            self._assert_security_headers(b_headers)
            self._assert_no_stack_trace(b_raw)
            self.assertEqual(b_body["error"]["code"], "ERR-400-INVALID-FIELD-NOTE")

        off_status, off_headers, off_body, _ = self._request_json(
            "POST",
            "/api/v1/device/connectivity",
            {"is_online": False},
        )
        self.assertEqual(off_status, 200)
        self._assert_security_headers(off_headers)
        self.assertFalse(off_body["is_online"])

        # 2. Capture brand-new offline note (201) and edit NOTE-SITE-001 offline (200).
        create_status, _, created, _ = self._request_json(
            "POST",
            "/api/v1/notes/local",
            {
                "note_id": "NOTE-OFFLINE-NEW-01",
                "site_code": "SUB-VALLE-08",
                "title": "Offline Substation Ground Grid Resistance",
                "body": "Fall-of-potential measurement recorded 0.82 Ohm while offline.",
                "severity": "INFO",
                "location": {"latitude": 3.4516, "longitude": -76.5320},
                "author_device_id": "device-ios-field-01",
            },
        )
        self.assertEqual(create_status, 201)
        self.assertEqual(created["note"]["note_id"], "NOTE-OFFLINE-NEW-01")
        self.assertEqual(created["note"]["version"], 1)
        self.assertEqual(created["note"]["sync_state"], "PENDING_PUSH")
        self.assertEqual(created["mutation"]["base_version"], 0)
        self.assertEqual(created["mutation"]["status"], "QUEUED")
        self.assertFalse(created["is_online"])

        edit_status, _, edited, _ = self._request_json(
            "POST",
            "/api/v1/notes/local",
            {
                "note_id": "NOTE-SITE-001",
                "title": "Offline Update: Bushing Thermal Reading 98.6 C",
                "body": "Second FLIR pass confirmed 98.6 C hotspot; load shed requested.",
                "severity": "CRITICAL",
            },
        )
        self.assertEqual(edit_status, 200)
        self.assertEqual(edited["note"]["version"], 2)
        self.assertEqual(edited["note"]["sync_state"], "PENDING_PUSH")
        self.assertEqual(edited["mutation"]["base_version"], 1)
        self.assertEqual(edited["mutation"]["status"], "QUEUED")

        q_status, _, queue_body, _ = self._request_json(
            "GET",
            "/api/v1/sync/queue?only_queued=true",
        )
        self.assertEqual(q_status, 200)
        self.assertEqual(queue_body["queued_count"], 2)
        self.assertEqual(
            [m["note_id"] for m in queue_body["mutations"]],
            ["NOTE-OFFLINE-NEW-01", "NOTE-SITE-001"],
        )

        # 3. Attempt sync while offline -> 503 Service Unavailable (ERR-503-DEVICE-OFFLINE)
        sync_off_status, sync_off_headers, sync_off_body, sync_off_raw = self._request_json(
            "POST",
            "/api/v1/sync/run",
            {},
        )
        self.assertEqual(sync_off_status, 503)
        self._assert_security_headers(sync_off_headers)
        self._assert_no_stack_trace(sync_off_raw)
        self.assertEqual(sync_off_body["error"]["code"], "ERR-503-DEVICE-OFFLINE")
        self.assertEqual(sync_off_body["error"]["type"], "OfflineNetworkError")

        # Queue MUST remain intact after 503 offline failure.
        _, _, queue_after_503, _ = self._request_json("GET", "/api/v1/sync/queue?only_queued=true")
        self.assertEqual(queue_after_503["queued_count"], 2)

        # 4. Simulate concurrent remote edit on NOTE-SITE-001 while device is offline.
        sim_status, _, sim_body, _ = self._request_json(
            "POST",
            "/api/v1/notes/remote/NOTE-SITE-001/simulate-edit",
            {
                "title": "Dispatch Update: Crew Bravo Assigned to Transformer",
                "body": "Crew Bravo departed depot with replacement 230 kV bushing.",
                "severity": "CRITICAL",
                "author_device_id": "dispatch-web-02",
            },
        )
        self.assertEqual(sim_status, 200)
        self.assertEqual(sim_body["remote_note"]["version"], 2)
        self.assertEqual(sim_body["simulated_by"], "dispatch-web-02")

        # 5. Reconnect online and execute bidirectional delta sync.
        on_status, _, on_body, _ = self._request_json(
            "POST",
            "/api/v1/device/connectivity",
            {"is_online": True},
        )
        self.assertEqual(on_status, 200)
        self.assertTrue(on_body["is_online"])

        sync_status, _, sync_body, _ = self._request_json("POST", "/api/v1/sync/run", {})
        self.assertEqual(sync_status, 200)
        self.assertEqual(sync_body["pushed_count"], 1)
        self.assertEqual(sync_body["conflicts_detected"], 1)
        self.assertEqual(sync_body["audit"]["status"], "PARTIAL_CONFLICT")
        self.assertEqual(len(sync_body["conflicts"]), 1)

        # Verify GET /api/v1/conflicts returns the unresolved conflict on NOTE-SITE-001.
        c_status, _, conflicts_body, _ = self._request_json(
            "GET",
            "/api/v1/conflicts?unresolved_only=true",
        )
        self.assertEqual(c_status, 200)
        self.assertEqual(conflicts_body["unresolved_count"], 1)
        conflict = conflicts_body["conflicts"][0]
        conflict_id = conflict["conflict_id"]
        self.assertEqual(conflict["note_id"], "NOTE-SITE-001")
        self.assertFalse(conflict["resolved"])
        self.assertEqual(
            conflict["local_note"]["title"],
            "Offline Update: Bushing Thermal Reading 98.6 C",
        )
        self.assertEqual(
            conflict["remote_note"]["title"],
            "Dispatch Update: Crew Bravo Assigned to Transformer",
        )

        # Verify newly captured offline note reached the remote store cleanly.
        _, _, remote_after_sync, _ = self._request_json("GET", "/api/v1/notes/remote")
        remote_by_id = {n["note_id"]: n for n in remote_after_sync["notes"]}
        self.assertIn("NOTE-OFFLINE-NEW-01", remote_by_id)
        self.assertEqual(remote_by_id["NOTE-OFFLINE-NEW-01"]["sync_state"], "SYNCED")
        self.assertEqual(remote_by_id["NOTE-OFFLINE-NEW-01"]["version"], 1)

        # 6. Resolve conflict: first invalid MANUAL_MERGE -> 422, then valid MANUAL_MERGE -> 200.
        bad_merge_status, bad_merge_headers, bad_merge_body, bad_merge_raw = self._request_json(
            "POST",
            f"/api/v1/conflicts/{conflict_id}/resolve",
            {
                "strategy": "MANUAL_MERGE",
                "merged_title": "Missing Body Should Fail",
                "merged_body": "   ",
            },
        )
        self.assertEqual(bad_merge_status, 422)
        self._assert_security_headers(bad_merge_headers)
        self._assert_no_stack_trace(bad_merge_raw)
        self.assertEqual(bad_merge_body["error"]["code"], "ERR-422-INVALID-RESOLUTION")

        merged_title = "Bushing Hotspot 98.6 C — Crew Bravo Dispatched"
        merged_body = (
            "Converged field FLIR reading (98.6 C, load shed requested) with "
            "Crew Bravo 230 kV replacement bushing dispatch."
        )
        res_status, _, res_body, _ = self._request_json(
            "POST",
            f"/api/v1/conflicts/{conflict_id}/resolve",
            {
                "strategy": "MANUAL_MERGE",
                "merged_title": merged_title,
                "merged_body": merged_body,
            },
        )
        self.assertEqual(res_status, 200)
        self.assertEqual(res_body["resolved_note"]["version"], 3)
        self.assertEqual(res_body["resolved_note"]["sync_state"], "SYNCED")
        self.assertEqual(res_body["resolved_note"]["title"], merged_title)
        self.assertEqual(res_body["resolved_note"]["body"], merged_body)
        self.assertTrue(res_body["conflict"]["resolved"])
        self.assertEqual(res_body["conflict"]["strategy"], "MANUAL_MERGE")

        # Attempting to re-resolve an already resolved conflict returns 422.
        re_status, _, re_body, re_raw = self._request_json(
            "POST",
            f"/api/v1/conflicts/{conflict_id}/resolve",
            {"strategy": "KEEP_LOCAL"},
        )
        self.assertEqual(re_status, 422)
        self._assert_no_stack_trace(re_raw)
        self.assertEqual(re_body["error"]["code"], "ERR-422-INVALID-RESOLUTION")

        # Verify zero unresolved conflicts, converged local & remote notes, and immutable audits.
        _, _, final_conflicts, _ = self._request_json(
            "GET",
            "/api/v1/conflicts?unresolved_only=true",
        )
        self.assertEqual(final_conflicts["unresolved_count"], 0)
        self.assertEqual(final_conflicts["conflicts"], [])

        _, _, final_local, _ = self._request_json("GET", "/api/v1/notes/local")
        _, _, final_remote, _ = self._request_json("GET", "/api/v1/notes/remote")
        local_map = {n["note_id"]: n for n in final_local["notes"]}
        remote_map = {n["note_id"]: n for n in final_remote["notes"]}
        self.assertEqual(local_map["NOTE-SITE-001"], remote_map["NOTE-SITE-001"])
        self.assertEqual(local_map["NOTE-SITE-001"]["version"], 3)
        self.assertEqual(local_map["NOTE-SITE-001"]["sync_state"], "SYNCED")
        self.assertEqual(local_map["NOTE-SITE-001"]["title"], merged_title)

        aud_status, _, audit_body, _ = self._request_json("GET", "/api/v1/sync/audit")
        self.assertEqual(aud_status, 200)
        self.assertGreaterEqual(audit_body["total"], 2)
        statuses = {entry["status"] for entry in audit_body["audits"]}
        self.assertIn("PARTIAL_CONFLICT", statuses)
        self.assertIn("RESOLVED_MANUAL_MERGE", statuses)

        _, _, final_health, _ = self._request_json("GET", "/api/v1/health")
        self.assertEqual(final_health["local_notes_count"], 6)
        self.assertEqual(final_health["remote_notes_count"], 6)
        self.assertEqual(final_health["queued_mutations_count"], 0)
        self.assertEqual(final_health["unresolved_conflicts_count"], 0)
        self.assertEqual(final_health["sync_audits_count"], 2)

    def test_delta_pull_converges_non_conflicting_remote_edit(self) -> None:
        sim_status, _, sim_body, _ = self._request_json(
            "POST",
            "/api/v1/notes/remote/NOTE-SITE-002/simulate-edit",
            {
                "title": "Remote Valve Actuator Serviced",
                "body": "Nitrogen pre-charge topped up to 145 bar by night shift.",
                "severity": "INFO",
            },
        )
        self.assertEqual(sim_status, 200)
        self.assertEqual(sim_body["remote_note"]["version"], 2)

        sync_status, _, sync_body, _ = self._request_json("POST", "/api/v1/sync/run", {})
        self.assertEqual(sync_status, 200)
        self.assertEqual(sync_body["pushed_count"], 0)
        self.assertEqual(sync_body["pulled_count"], 1)
        self.assertEqual(sync_body["conflicts_detected"], 0)
        self.assertEqual(sync_body["audit"]["status"], "SUCCESS")

        _, _, local_body, _ = self._request_json("GET", "/api/v1/notes/local")
        note2 = next(n for n in local_body["notes"] if n["note_id"] == "NOTE-SITE-002")
        self.assertEqual(note2["version"], 2)
        self.assertEqual(note2["title"], "Remote Valve Actuator Serviced")
        self.assertEqual(note2["sync_state"], "SYNCED")

    def test_conflict_resolution_offline_503_and_keep_local_accept_remote_strategies(self) -> None:
        # Go offline, edit NOTE-SITE-003 and NOTE-SITE-004 locally.
        self._request_json("POST", "/api/v1/device/connectivity", {"is_online": False})
        self._request_json(
            "POST",
            "/api/v1/notes/local",
            {
                "note_id": "NOTE-SITE-003",
                "title": "Local Keep Title v2",
                "body": "Local Keep Body v2",
            },
        )
        self._request_json(
            "POST",
            "/api/v1/notes/local",
            {
                "note_id": "NOTE-SITE-004",
                "title": "Local Overwritten Title v2",
                "body": "Local Overwritten Body v2",
            },
        )

        # Simulate concurrent remote edits on both notes.
        self._request_json(
            "POST",
            "/api/v1/notes/remote/NOTE-SITE-003/simulate-edit",
            {"title": "Remote Superseded Title v2", "body": "Remote Superseded Body v2"},
        )
        self._request_json(
            "POST",
            "/api/v1/notes/remote/NOTE-SITE-004/simulate-edit",
            {"title": "Remote Accepted Title v2", "body": "Remote Accepted Body v2"},
        )

        # Go online and sync -> 2 conflicts detected.
        self._request_json("POST", "/api/v1/device/connectivity", {"is_online": True})
        st_sync, _, sync_body, _ = self._request_json("POST", "/api/v1/sync/run", {})
        self.assertEqual(st_sync, 200)
        self.assertEqual(sync_body["conflicts_detected"], 2)
        conflicts_by_note = {c["note_id"]: c["conflict_id"] for c in sync_body["conflicts"]}

        # Switching offline before resolving MUST return 503 Service Unavailable.
        self._request_json("POST", "/api/v1/device/connectivity", {"is_online": False})
        st_503, h_503, body_503, raw_503 = self._request_json(
            "POST",
            f"/api/v1/conflicts/{conflicts_by_note['NOTE-SITE-003']}/resolve",
            {"strategy": "KEEP_LOCAL"},
        )
        self.assertEqual(st_503, 503)
        self._assert_security_headers(h_503)
        self._assert_no_stack_trace(raw_503)
        self.assertEqual(body_503["error"]["code"], "ERR-503-DEVICE-OFFLINE")

        # Reconnect and resolve NOTE-SITE-003 with KEEP_LOCAL and NOTE-SITE-004 with ACCEPT_REMOTE.
        self._request_json("POST", "/api/v1/device/connectivity", {"is_online": True})
        st_kl, _, body_kl, _ = self._request_json(
            "POST",
            f"/api/v1/conflicts/{conflicts_by_note['NOTE-SITE-003']}/resolve",
            {"strategy": "KEEP_LOCAL"},
        )
        self.assertEqual(st_kl, 200)
        self.assertEqual(body_kl["resolved_note"]["title"], "Local Keep Title v2")
        self.assertEqual(body_kl["resolved_note"]["version"], 3)
        self.assertEqual(body_kl["resolved_note"]["sync_state"], "SYNCED")

        st_ar, _, body_ar, _ = self._request_json(
            "POST",
            f"/api/v1/conflicts/{conflicts_by_note['NOTE-SITE-004']}/resolve",
            {"strategy": "ACCEPT_REMOTE"},
        )
        self.assertEqual(st_ar, 200)
        self.assertEqual(body_ar["resolved_note"]["title"], "Remote Accepted Title v2")
        self.assertEqual(body_ar["resolved_note"]["version"], 3)
        self.assertEqual(body_ar["resolved_note"]["sync_state"], "SYNCED")

        # Both local and remote stores must converge identically with zero unresolved conflicts.
        _, _, rem_body, _ = self._request_json("GET", "/api/v1/notes/remote")
        _, _, loc_body, _ = self._request_json("GET", "/api/v1/notes/local")
        rem_map = {n["note_id"]: n for n in rem_body["notes"]}
        loc_map = {n["note_id"]: n for n in loc_body["notes"]}
        self.assertEqual(loc_map["NOTE-SITE-003"], rem_map["NOTE-SITE-003"])
        self.assertEqual(loc_map["NOTE-SITE-004"], rem_map["NOTE-SITE-004"])
        _, _, h_after, _ = self._request_json("GET", "/api/v1/health")
        self.assertEqual(h_after["unresolved_conflicts_count"], 0)
        self.assertEqual(h_after["queued_mutations_count"], 0)


class TestHttpErrorCodesAndSecurityBoundaries(_LiveServerTestCase):
    """Verify 400, 404, 405, 413, 422, 500, 503 status codes and zero stack-trace leakage."""

    def test_400_bad_request_variants(self) -> None:
        # Malformed JSON syntax
        st1, h1, raw1 = self._request_raw(
            "POST",
            "/api/v1/notes/local",
            body=b"{not-valid-json",
        )
        self.assertEqual(st1, 400)
        self._assert_security_headers(h1)
        self._assert_no_stack_trace(raw1)
        self.assertEqual(json.loads(raw1)["error"]["code"], "ERR-400-MALFORMED-JSON")

        # JSON array instead of JSON object
        st2, h2, body2, raw2 = self._request_json("POST", "/api/v1/notes/local", ["invalid"])
        self.assertEqual(st2, 400)
        self._assert_security_headers(h2)
        self._assert_no_stack_trace(raw2)
        self.assertEqual(body2["error"]["code"], "ERR-400-MALFORMED-JSON")

        # Out-of-range coordinate violates domain invariant -> 400
        st3, h3, body3, raw3 = self._request_json(
            "POST",
            "/api/v1/notes/local",
            {
                "note_id": "NOTE-BAD-GEO",
                "site_code": "SUB-01",
                "title": "Bad GPS",
                "body": "Latitude 95 is out of WGS84 bounds.",
                "severity": "INFO",
                "location": {"latitude": 95.0, "longitude": -74.0},
            },
        )
        self.assertEqual(st3, 400)
        self._assert_security_headers(h3)
        self._assert_no_stack_trace(raw3)
        self.assertEqual(body3["error"]["code"], "ERR-400-INVALID-FIELD-NOTE")

        # Invalid query parameters -> 400
        for bad_url in (
            "/api/v1/sync/audit?limit=0",
            "/api/v1/sync/audit?limit=9999",
            "/api/v1/sync/audit?limit=abc",
            "/api/v1/conflicts?unresolved_only=invalid_bool",
        ):
            with self.subTest(url=bad_url):
                st, hdrs, err_body, raw = self._request_json("GET", bad_url)
                self.assertEqual(st, 400)
                self._assert_security_headers(hdrs)
                self._assert_no_stack_trace(raw)
                self.assertEqual(err_body["error"]["code"], "ERR-400-INVALID-FIELD-NOTE")

    def test_404_not_found_routes_and_entities(self) -> None:
        st_route, h_route, b_route, r_route = self._request_json("GET", "/api/v1/unknown-endpoint")
        self.assertEqual(st_route, 404)
        self._assert_security_headers(h_route)
        self._assert_no_stack_trace(r_route)
        self.assertEqual(b_route["error"]["code"], "ERR-404-ROUTE-NOT-FOUND")

        st_note, h_note, b_note, r_note = self._request_json(
            "POST",
            "/api/v1/notes/remote/NOTE-DOES-NOT-EXIST/simulate-edit",
            {"title": "Missing", "body": "Should 404"},
        )
        self.assertEqual(st_note, 404)
        self._assert_security_headers(h_note)
        self._assert_no_stack_trace(r_note)
        self.assertEqual(b_note["error"]["code"], "ERR-404-NOTE-NOT-FOUND")

        st_conf, h_conf, b_conf, r_conf = self._request_json(
            "POST",
            "/api/v1/conflicts/conf-missing-999/resolve",
            {"strategy": "KEEP_LOCAL"},
        )
        self.assertEqual(st_conf, 404)
        self._assert_security_headers(h_conf)
        self._assert_no_stack_trace(r_conf)
        self.assertEqual(b_conf["error"]["code"], "ERR-404-NOTE-NOT-FOUND")

    def test_405_method_not_allowed_with_allow_header(self) -> None:
        cases = [
            ("PUT", "/api/v1/notes/local"),
            ("DELETE", "/api/v1/notes/local"),
            ("PATCH", "/api/v1/notes/local"),
            ("GET", "/api/v1/sync/run"),
            ("POST", "/api/v1/health"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, headers, raw = self._request_raw(method, path)
                self.assertEqual(status, 405)
                self._assert_security_headers(headers)
                self._assert_no_stack_trace(raw)
                self.assertIn("allow", headers)
                body = json.loads(raw)
                self.assertEqual(body["error"]["code"], "ERR-405-METHOD-NOT-ALLOWED")

    def test_413_payload_too_large_over_64kb(self) -> None:
        oversized_payload = json.dumps(
            {
                "note_id": "NOTE-OVERSIZED",
                "site_code": "SUB-01",
                "title": "Oversized Payload Test",
                "body": "X" * (MAX_JSON_BODY_BYTES + 2048),
                "severity": "INFO",
                "location": {"latitude": 4.711, "longitude": -74.0721},
            }
        ).encode("utf-8")
        self.assertGreater(len(oversized_payload), MAX_JSON_BODY_BYTES)

        status, headers, raw = self._request_raw(
            "POST",
            "/api/v1/notes/local",
            body=oversized_payload,
        )
        self.assertEqual(status, 413)
        self._assert_security_headers(headers)
        self._assert_no_stack_trace(raw)
        body = json.loads(raw)
        self.assertEqual(body["error"]["code"], "ERR-413-PAYLOAD-TOO-LARGE")
        self.assertEqual(body["error"]["details"]["max_bytes"], MAX_JSON_BODY_BYTES)

    def test_exception_mapper_409_and_500_never_leak_internal_details(self) -> None:
        code_409, body_409 = map_exception_to_http(
            SyncConflictError("NOTE-1", local_version=1, server_version=2)
        )
        self.assertEqual(code_409, 409)
        self.assertEqual(body_409["error"]["code"], "ERR-409-SYNC-CONFLICT")

        code_500, body_500 = map_exception_to_http(
            RuntimeError("sqlite3.OperationalError: secret /var/db/prod.sqlite3")
        )
        self.assertEqual(code_500, 500)
        self.assertEqual(body_500["error"]["code"], "ERR-500-INTERNAL-SERVER-ERROR")
        serialized = json.dumps(body_500)
        self.assertNotIn("sqlite3", serialized)
        self.assertNotIn("/var/db/prod.sqlite3", serialized)
        self._assert_no_stack_trace(serialized)


if __name__ == "__main__":
    unittest.main()
