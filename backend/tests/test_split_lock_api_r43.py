from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.service import CoreService


PASSWORD = "split lock API regression password"


class EmptyBrowser:
    """Local inventory double; these API tests never contact a browser provider."""

    def list_all_windows(self, *, name: str = "") -> dict[str, Any]:
        del name
        return {
            "windows": [],
            "total": 0,
            "provider_success": True,
            "stale": False,
            "connection": {"state": "connected", "connected": True},
        }


class SplitLockApiR43Tests(unittest.TestCase):
    """Exercise authenticated HTTP parsing, persistence and owner boundaries."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "split-lock-api.sqlite3"
        self.database = Database(self.db_path)
        self.database.initialize()
        self.service = CoreService(self.database, session_hours=1)
        self.settings = Settings(
            startup_token="r43-split-lock-test-startup-token-long-enough",
            database_path=self.db_path,
            data_dir=self.data_dir,
        )
        self.headers: dict[str, dict[str, str]] = {}
        self.owner_ids: dict[str, str] = {}
        for username in ("lock-owner", "other-owner"):
            user = self.service.register_user(username, PASSWORD)
            self.owner_ids[username] = user["id"]
            login = self.service.login(username, PASSWORD)
            self.headers[username] = {
                "X-Startup-Token": self.settings.startup_token,
                "Authorization": f"Bearer {login['token']}",
            }
        self._open_client()

    def _open_client(self) -> None:
        app = create_app(
            self.settings, database=self.database, bitbrowser=EmptyBrowser()
        )
        # The initialized real database and ASGI routes are exercised directly.
        # Omitting the lifespan prevents unrelated scheduler/cloud workers from
        # starting; no authentication or application endpoint is replaced.
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _reopen_client(self) -> None:
        self.client.close()
        self.database = Database(self.db_path)
        self.database.initialize()
        self._open_client()

    def _command(
        self, kind: str, payload: dict[str, Any], *, owner: str = "lock-owner"
    ) -> Any:
        return self.client.post(
            "/api/workbench/commands",
            headers=self.headers[owner],
            json={"type": kind, "payload": payload},
        )

    def _snapshot(self, *, owner: str = "lock-owner") -> dict[str, Any]:
        response = self.client.get(
            "/api/workbench/snapshot?limit=20&history_limit=1",
            headers=self.headers[owner],
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _add(self, username: str = "waiting.locked", *, owner: str = "lock-owner") -> str:
        response = self._command(
            "split_waiting_add", {"targets": [username]}, owner=owner
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]["accepted_ids"][0]

    def _candidate(self, candidate_id: str, *, owner: str = "lock-owner") -> dict[str, Any]:
        rows = self._snapshot(owner=owner)["split_candidates"]
        return next(row for row in rows if row["id"] == candidate_id)

    def test_candidate_command_lock_persists_and_rest_unlock_is_visible(self) -> None:
        candidate_id = self._add()
        before = self._snapshot()
        self.assertIs(False, self._candidate(candidate_id)["locked"])
        response = self._command(
            "split_waiting_lock", {"candidate_id": candidate_id, "locked": True}
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(candidate_id, response.json()["result"]["candidate"]["id"])
        self.assertIs(True, response.json()["result"]["candidate"]["locked"])
        self.assertGreater(response.json()["snapshot_seq"], before["snapshot_seq"])
        self.assertIs(True, self._candidate(candidate_id)["locked"])
        self.assertIs(False, self._snapshot()["split_claim_locked"])

        self._reopen_client()
        self.assertIs(True, self._candidate(candidate_id)["locked"])
        unlocked = self.client.patch(
            f"/api/split-candidates/{candidate_id}/lock",
            headers=self.headers["lock-owner"],
            json={"locked": False},
        )
        self.assertEqual(200, unlocked.status_code, unlocked.text)
        self.assertIs(False, unlocked.json()["candidate"]["locked"])
        self.assertIs(False, self._candidate(candidate_id)["locked"])

        relocked = self.client.patch(
            f"/api/split-candidates/{candidate_id}/lock",
            headers=self.headers["lock-owner"],
            json={"locked": True},
        )
        self.assertEqual(200, relocked.status_code, relocked.text)
        self.assertIs(True, relocked.json()["candidate"]["locked"])
        command_unlock = self._command(
            "split_waiting_lock", {"candidate_id": candidate_id, "locked": False}
        )
        self.assertEqual(200, command_unlock.status_code, command_unlock.text)
        self.assertIs(False, command_unlock.json()["result"]["candidate"]["locked"])
        self.assertIs(False, self._candidate(candidate_id)["locked"])

    def test_owner_claim_lock_persists_and_is_independent_of_candidate_locks(self) -> None:
        candidate_id = self._add()
        candidate_lock = self._command(
            "split_waiting_lock", {"candidate_id": candidate_id, "locked": True}
        )
        self.assertEqual(200, candidate_lock.status_code, candidate_lock.text)
        locked = self._command("split_claim_lock", {"locked": True})
        self.assertEqual(200, locked.status_code, locked.text)
        self.assertIs(True, locked.json()["result"]["locked"])
        self.assertIs(True, self._snapshot()["split_claim_locked"])
        self.assertIs(False, self._snapshot(owner="other-owner")["split_claim_locked"])

        self._reopen_client()
        self.assertIs(True, self._snapshot()["split_claim_locked"])
        self.assertIs(True, self._candidate(candidate_id)["locked"])
        unlocked = self.client.put(
            "/api/split-candidates/claim-lock",
            headers=self.headers["lock-owner"],
            json={"locked": False},
        )
        self.assertEqual(200, unlocked.status_code, unlocked.text)
        self.assertIs(False, unlocked.json()["locked"])
        self.assertIs(False, self._snapshot()["split_claim_locked"])
        self.assertIs(True, self._candidate(candidate_id)["locked"])

        other_lock = self._command("split_claim_lock", {"locked": True}, owner="other-owner")
        self.assertEqual(200, other_lock.status_code, other_lock.text)
        self.assertIs(True, self._snapshot(owner="other-owner")["split_claim_locked"])
        self.assertIs(False, self._snapshot()["split_claim_locked"])

        relocked = self.client.put(
            "/api/split-candidates/claim-lock",
            headers=self.headers["lock-owner"],
            json={"locked": True},
        )
        self.assertEqual(200, relocked.status_code, relocked.text)
        self.assertIs(True, relocked.json()["locked"])
        command_unlock = self._command("split_claim_lock", {"locked": False})
        self.assertEqual(200, command_unlock.status_code, command_unlock.text)
        self.assertIs(False, command_unlock.json()["result"]["locked"])
        self.assertIs(False, self._snapshot()["split_claim_locked"])
        self.assertIs(True, self._snapshot(owner="other-owner")["split_claim_locked"])

    def test_foreign_candidate_cannot_be_unlocked_by_command_or_rest(self) -> None:
        candidate_id = self._add()
        locked = self._command(
            "split_waiting_lock", {"candidate_id": candidate_id, "locked": True}
        )
        self.assertEqual(200, locked.status_code, locked.text)
        self.assertEqual([], self._snapshot(owner="other-owner")["split_candidates"])

        command = self._command(
            "split_waiting_lock",
            {"candidate_id": candidate_id, "locked": False},
            owner="other-owner",
        )
        self.assertEqual(404, command.status_code, command.text)
        rest = self.client.patch(
            f"/api/split-candidates/{candidate_id}/lock",
            headers=self.headers["other-owner"],
            json={"locked": False},
        )
        self.assertEqual(404, rest.status_code, rest.text)
        self.assertIs(True, self._candidate(candidate_id)["locked"])

    def test_http_locks_block_new_claims_and_allow_current_target_to_finish(self) -> None:
        owner_id = self.owner_ids["lock-owner"]
        self._add("claim.current")
        task = self.service.create_task(
            owner_id,
            name="HTTP lock dispatch integration",
            modes=["followers"],
            targets=["seed.account"],
            window_ids=["window-a"],
            settings={"live_queue_enabled": True, "location_enabled": True},
        )
        self.service.set_target_runtime_status(
            owner_id, task["id"], task["targets"][0]["id"], "completed"
        )
        self.service.set_task_runtime_status(owner_id, task["id"], "running")
        current = self.service.claim_next_split_candidate(owner_id, task["id"], "window-a")
        self.assertIsNotNone(current)
        self.assertEqual("claim.current", current["username"])

        waiting_locked_id = self._add("claim.individually.locked")
        self._add("claim.available")
        individual_lock = self._command(
            "split_waiting_lock", {"candidate_id": waiting_locked_id, "locked": True}
        )
        self.assertEqual(200, individual_lock.status_code, individual_lock.text)
        owner_lock = self._command("split_claim_lock", {"locked": True})
        self.assertEqual(200, owner_lock.status_code, owner_lock.text)
        self.assertIsNone(
            self.service.claim_next_split_candidate(owner_id, task["id"], "window-a")
        )
        current_after_lock = next(
            row for row in self.service.get_task(owner_id, task["id"])["targets"]
            if row["id"] == current["id"]
        )
        self.assertEqual("running", current_after_lock["status"])
        self.service.set_target_runtime_status(owner_id, task["id"], current["id"], "completed")
        current_after_finish = next(
            row for row in self.service.get_task(owner_id, task["id"])["targets"]
            if row["id"] == current["id"]
        )
        self.assertEqual("completed", current_after_finish["status"])
        self.assertIsNone(
            self.service.claim_next_split_candidate(owner_id, task["id"], "window-a")
        )

        owner_unlock = self._command("split_claim_lock", {"locked": False})
        self.assertEqual(200, owner_unlock.status_code, owner_unlock.text)
        available = self.service.claim_next_split_candidate(owner_id, task["id"], "window-a")
        self.assertIsNotNone(available)
        self.assertEqual("claim.available", available["username"])
        self.service.set_target_runtime_status(owner_id, task["id"], available["id"], "completed")
        self.assertIsNone(
            self.service.claim_next_split_candidate(owner_id, task["id"], "window-a")
        )
        individual_unlock = self._command(
            "split_waiting_lock", {"candidate_id": waiting_locked_id, "locked": False}
        )
        self.assertEqual(200, individual_unlock.status_code, individual_unlock.text)
        resumed = self.service.claim_next_split_candidate(owner_id, task["id"], "window-a")
        self.assertIsNotNone(resumed)
        self.assertEqual("claim.individually.locked", resumed["username"])

    def test_candidate_missing_or_nonboolean_lock_is_rejected_without_unlocking(self) -> None:
        candidate_id = self._add()
        locked = self._command(
            "split_waiting_lock", {"candidate_id": candidate_id, "locked": True}
        )
        self.assertEqual(200, locked.status_code, locked.text)
        invalid_bodies = [{}, *({"locked": value} for value in (None, "false", "true", 0, 1, [], {}))]
        for payload in invalid_bodies:
            with self.subTest(payload=payload):
                command = self._command(
                    "split_waiting_lock", {"candidate_id": candidate_id, **payload}
                )
                self.assertEqual(422, command.status_code, command.text)
                rest = self.client.patch(
                    f"/api/split-candidates/{candidate_id}/lock",
                    headers=self.headers["lock-owner"],
                    json=payload,
                )
                self.assertEqual(422, rest.status_code, rest.text)
                self.assertIs(True, self._candidate(candidate_id)["locked"])

    def test_total_missing_or_nonboolean_lock_is_rejected_without_unlocking(self) -> None:
        locked = self._command("split_claim_lock", {"locked": True})
        self.assertEqual(200, locked.status_code, locked.text)
        invalid_bodies = [{}, *({"locked": value} for value in (None, "false", "true", 0, 1, [], {}))]
        for payload in invalid_bodies:
            with self.subTest(payload=payload):
                command = self._command("split_claim_lock", payload)
                self.assertEqual(422, command.status_code, command.text)
                rest = self.client.put(
                    "/api/split-candidates/claim-lock",
                    headers=self.headers["lock-owner"],
                    json=payload,
                )
                self.assertEqual(422, rest.status_code, rest.text)
                self.assertIs(True, self._snapshot()["split_claim_locked"])


if __name__ == "__main__":
    unittest.main()
