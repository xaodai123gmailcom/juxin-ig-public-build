from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from app.relationship_diagnostics import RelationshipDiagnostics, RelationshipDiagnosticStore, _decode_json_body


class Events:
    def __init__(self):
        self.listeners = []

    def on(self, event, listener):
        assert event == "response"
        self.listeners.append(listener)

    def remove_listener(self, event, listener):
        self.listeners.remove(listener)

    def emit(self, response):
        for listener in list(self.listeners):
            listener(response)


class Response:
    def __init__(self, page, *, url="https://www.instagram.com/api/graphql", payload=None, delay=0):
        self.url, self.status, self.delay = url, 200, delay
        self.headers = {"content-type": "application/json", "set-cookie": "NEVER_SAVE_COOKIE"}
        self.request = SimpleNamespace(frame=SimpleNamespace(page=page), resource_type="fetch",
                                       post_data=json.dumps({"variables": {"id": "12345", "after": "PAGE_CURSOR"},
                                                             "access_token": "NEVER_SAVE_REQUEST_TOKEN"}))
        self.payload = payload if payload is not None else {
            "data": {"user": {"id": "12345", "edge_follow": {
                "count": 151, "edges": [{"node": {"id": "222", "username": "alice"}}],
                "page_info": {"has_next_page": True, "end_cursor": "PAGE_CURSOR"}}}},
            "access_token": "NEVER_SAVE_TOKEN", "message": "NEVER_SAVE_DM",
            "arbitrary_string": "NEVER_SAVE_ARBITRARY_TEXT",
        }
        self.body_reads = 0

    async def body(self):
        self.body_reads += 1
        await asyncio.sleep(self.delay)
        return json.dumps(self.payload).encode()


class RelationshipDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_json_prefixes_and_streams_are_parsed_without_executing_text(self):
        self.assertEqual({"data": {}}, _decode_json_body(b'for (;;);{"data":{}}'))
        self.assertEqual({"data": {}}, _decode_json_body(b")]}'\n{\"data\":{}}"))
        self.assertEqual(2, len(_decode_json_body(b'{"a":1}\n{"a":2}')['_documents']))
        with self.assertRaises(ValueError):
            _decode_json_body(b'alert("not JSON");')

    async def test_passive_capture_keeps_structure_and_pagination_equality_but_redacts_content(self):
        page, events = object(), Events()
        diagnostic = RelationshipDiagnostics(lambda: page)
        diagnostic.start(events)
        diagnostic.attempt = 1
        events.emit(Response(page))
        diagnostic.record_attempt(151, {"alice"})
        await diagnostic.stop()
        report = diagnostic.report()
        serialized = json.dumps(report)
        for value in ("NEVER_SAVE", "alice", "PAGE_CURSOR", '"12345"', '"222"'):
            self.assertNotIn(value, serialized)
        self.assertEqual(1, report["counters"]["captured"])
        sample = report["response_samples"][0]
        self.assertEqual(1, sample["attempt"])
        relation = sample["response_shape"]["data"]["user"]["edge_follow"]
        self.assertEqual(151, relation["count"])
        self.assertIs(True, relation["page_info"]["has_next_page"])
        self.assertEqual(sample["request_shape"]["variables"]["after"]["_ref"],
                         relation["page_info"]["end_cursor"]["_ref"])
        self.assertEqual(report["attempts"][0]["member_refs"][0],
                         relation["edges"]["_items"][0]["node"]["username"]["_ref"])
        self.assertEqual([], events.listeners)

    async def test_context_listener_tracks_replacement_page_and_excludes_other_tabs_and_dm(self):
        old, new = object(), object()
        current = [old]
        events = Events()
        diagnostic = RelationshipDiagnostics(lambda: current[0])
        diagnostic.start(events)
        rejected = [Response(new), Response(old, url="https://www.instagram.com.evil.invalid/api/graphql"),
                    Response(old, url="https://www.instagram.com/api/v1/direct_v2/inbox/")]
        for response in rejected:
            events.emit(response)
        events.emit(Response(old))
        current[0] = new
        ignored_old = Response(old)
        events.emit(ignored_old)
        events.emit(Response(new))
        await diagnostic.stop()
        after_stop = Response(new)
        events.emit(after_stop)
        self.assertEqual(2, diagnostic.report()["counters"]["captured"])
        self.assertTrue(all(response.body_reads == 0 for response in [*rejected, ignored_old, after_stop]))

    async def test_large_responses_and_slow_listener_work_are_bounded(self):
        page, events = object(), Events()
        diagnostic = RelationshipDiagnostics(lambda: page)
        diagnostic.start(events)
        oversized = Response(page)
        oversized.headers["content-length"] = str(diagnostic.MAX_BODY_BYTES + 1)
        events.emit(oversized)
        responses = [Response(page, delay=0.01) for _ in range(50)]
        for response in responses:
            events.emit(response)
        await diagnostic.stop()
        self.assertEqual(0, oversized.body_reads)
        self.assertEqual(diagnostic.MAX_PENDING, sum(response.body_reads for response in responses))
        self.assertEqual(46, diagnostic.counters["busy_skipped"])

    async def test_retains_first_and_last_samples_with_a_visible_drop_count(self):
        page, events = object(), Events()
        diagnostic = RelationshipDiagnostics(lambda: page)
        diagnostic.start(events)
        for _ in range(40):
            events.emit(Response(page))
            await asyncio.gather(*tuple(diagnostic.pending))
        await diagnostic.stop()
        report = diagnostic.report()
        self.assertEqual(32, len(report["response_samples"]))
        self.assertEqual(8, report["samples_dropped"])
        self.assertEqual(1, report["response_samples"][0]["sequence"])
        self.assertEqual(40, report["response_samples"][-1]["sequence"])

    async def test_response_failure_does_not_fail_scan_and_does_not_export_exception_text(self):
        page, events = object(), Events()
        diagnostic = RelationshipDiagnostics(lambda: page)
        diagnostic.start(events)
        response = Response(page)
        async def failed_body():
            raise RuntimeError("NEVER_SAVE_EXCEPTION_TOKEN")
        response.body = failed_body
        events.emit(response)
        await diagnostic.stop()
        self.assertEqual(1, diagnostic.counters["body_unavailable"])
        self.assertNotIn("NEVER_SAVE", json.dumps(diagnostic.report()))

    def test_payload_bounds_are_explicit_and_do_not_look_like_complete_lists(self):
        diagnostic = RelationshipDiagnostics(lambda: None)
        result = diagnostic.sanitize({"users": [{"username": str(i)} for i in range(3000)]})
        self.assertEqual(3000, result["users"]["_array_length"])
        self.assertTrue(result["users"]["_truncated"])
        self.assertEqual(100, len(result["users"]["_items"]))
        self.assertNotEqual(diagnostic.fingerprint("alice"), RelationshipDiagnostics(lambda: None).fingerprint("alice"))


class DiagnosticStoreTests(unittest.TestCase):
    def test_export_api_requires_session_and_cannot_select_another_owner(self):
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.database import Database
        from app.main import create_app
        from app.service import CoreService

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "collector.sqlite3")
            database.initialize()
            service = CoreService(database, session_hours=1)
            owner = service.register_user("diagnostic-owner", "correct horse battery staple")
            other = service.register_user("diagnostic-other", "correct horse battery staple")
            token = service.login("diagnostic-owner", "correct horse battery staple")["token"]
            settings = Settings(startup_token="diagnostic-startup-token-long-enough", database_path=database.path, data_dir=root)
            store = RelationshipDiagnosticStore(root / "follow-monitor-diagnostics")
            report = RelationshipDiagnostics(lambda: None).report()
            store.save(owner["id"], "window-a", {**report, "outcome": "own-report"})
            store.save(other["id"], "window-b", {**report, "outcome": "private-other-report"})
            with TestClient(create_app(settings, database=database, bitbrowser=SimpleNamespace())) as client:
                headers = {"X-Startup-Token": settings.startup_token}
                self.assertEqual(401, client.get("/api/follow-monitor/diagnostics", headers=headers).status_code)
                headers["Authorization"] = f"Bearer {token}"
                response = client.get("/api/follow-monitor/diagnostics", headers=headers,
                                      params={"owner_id": other["id"]})
                self.assertEqual(200, response.status_code)
                self.assertEqual(["own-report"], [r["outcome"] for r in response.json()["reports"]])

    def test_owner_isolation_and_replacement_of_same_window_without_touching_other_data(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RelationshipDiagnosticStore(Path(temp))
            report = RelationshipDiagnostics(lambda: None).report()
            store.save("owner-a", "window", {**report, "outcome": "old"})
            store.save("owner-b", "window", {**report, "outcome": "other-owner"})
            store.save("owner-a", "window", {**report, "outcome": "new"})
            self.assertEqual(["new"], [r["outcome"] for r in store.export("owner-a")["reports"]])
            self.assertEqual(["other-owner"], [r["outcome"] for r in store.export("owner-b")["reports"]])
            self.assertEqual([], store.export("unknown")["reports"])

    def test_storage_retention_and_disk_errors_do_not_break_a_check(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RelationshipDiagnosticStore(Path(temp))
            for i in range(15):
                store.save("owner", f"window-{i}", RelationshipDiagnostics(lambda: None).report())
            self.assertEqual(10, len(store.export("owner")["reports"]))
            invalid_root = Path(temp) / "not-a-directory"
            invalid_root.write_text("existing data")
            RelationshipDiagnosticStore(invalid_root).save("owner", "window", {"report": "test"})
            self.assertEqual("existing data", invalid_root.read_text())
