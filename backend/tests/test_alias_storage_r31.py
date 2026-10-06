"""Stable-identity repair must keep linear work and immutable saved evidence."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.service import CoreService
from test_spool_performance_r30 import CountingDatabase, STAMP, seed_task


def seed_saved_identities(database, count, *, separate=False, reviewed=True, reverse=True, group_size=None):
    """Reverse lexical/discovery order to expose correlated earlier-row scans."""
    names = [f"alias{index:06d}" for index in range(count)]
    def account_for(index):
        if group_size is not None:
            return f"identity{index // group_size:06d}"
        return f"identity{index:06d}" if separate else "identity"

    identities = list({account_for(index): name for index, name in enumerate(names)}.items())
    with database.write() as connection:
        connection.executemany(
            "INSERT INTO instagram_accounts(id,current_username_norm,current_username_display,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?)",
            ((account, name, name, STAMP, STAMP) for account, name in identities),
        )
        connection.executemany(
            "INSERT INTO instagram_username_aliases(account_id,username_norm,first_seen_at,last_seen_at) VALUES(?,?,?,?)",
            ((account_for(index), name, STAMP, STAMP)
             for index, name in enumerate(names)),
        )
        connection.executemany(
            "INSERT INTO global_seen(account_id,sources_json,first_seen_at,last_seen_at) VALUES(?,?,?,?)",
            ((account, '["followers"]', STAMP, STAMP) for account, name in identities),
        )
        connection.executemany(
            "INSERT INTO workbench_identity_claims(account_id,claimed_by_user_id,source,source_target,claimed_at) "
            "VALUES(?,'owner',?,'target',?)",
            ((account, "followers" if reviewed else "manual", STAMP) for account, name in identities),
        )
        connection.executemany(
            "INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,created_at,updated_at) "
            "VALUES(?,'task','target',?,'[\"followers\"]','private','{\"marker\":\"original\"}','{}',?,?)",
            ((f"result{index}", account, STAMP, STAMP) for index, (account, name) in enumerate(identities)),
        )
        if reviewed:
            connection.executemany(
                "INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,status,profile_json,screening_json,source_mode,source_target,created_at,updated_at) "
                "VALUES(?,'owner',?,'private',?,'{\"marker\":\"immutable_review\"}','{}','followers','target',?,?)",
                ((f"review{index}", account, "approved" if index % 2 else "rejected", STAMP, STAMP)
                 for index, (account, name) in enumerate(identities)),
            )
        connection.executemany(
            "INSERT INTO task_mode_candidates(target_id,mode,username_norm,username_display,discovery_order,discovered_at,updated_at) "
            "VALUES('target','followers',?,?,?,?,?)",
            ((name, name, index + 1, STAMP, STAMP)
             for index, name in enumerate(reversed(names) if reverse else names)),
        )
    return names


class AliasStorageR31Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_number = 0

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, count, *, separate=False, recognition=True, reviewed=True, reverse=True, group_size=None):
        self.database_number += 1
        database = CountingDatabase(Path(self.temp.name) / f"aliases-{self.database_number}.sqlite3")
        database.initialize()
        seed_task(database, recognition=recognition)
        names = seed_saved_identities(database, count, separate=separate, reviewed=reviewed,
                                      reverse=reverse, group_size=group_size)
        return database, CoreService(database), names

    @staticmethod
    def evidence(database):
        with database.read() as connection:
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("task_results", "workbench_candidates", "global_seen", "workbench_identity_claims")
            }

    def test_scoped_reverse_alias_repair_has_linear_work_across_parameter_batches(self):
        for reverse in (False, True):
            for recognition in (False, True):
                samples = []
                for count in (100, 1200):
                    database, service, names = self.prepare(count, recognition=recognition, reverse=reverse)
                    before = self.evidence(database)
                    stats, steps = database.measure(lambda: service.reconcile_task_mode_candidates(
                        "owner", "task", "target", "followers", usernames=[names[0]],
                    ))
                    self.assertEqual({"total": count, "pending": 0, "recorded": 1, "deduped": count - 1}, stats)
                    with database.read() as connection:
                        recorded = connection.execute(
                            "SELECT username_norm FROM task_mode_candidates WHERE state='recorded'"
                        ).fetchall()
                    self.assertEqual([names[-1] if reverse else names[0]], [row[0] for row in recorded])
                    self.assertEqual(before, self.evidence(database))
                    samples.append(steps)
                self.assertLess(samples[1], samples[0] * 16 + 2000, (recognition, reverse, samples))

    def test_multiple_identities_keep_one_recorded_alias_each_across_parameter_batches(self):
        for reverse in (False, True):
            for recognition in (False, True):
                database, service, names = self.prepare(
                    1200, group_size=400, recognition=recognition, reverse=reverse
                )
                before = self.evidence(database)
                stats = service.reconcile_task_mode_candidates(
                    "owner", "task", "target", "followers",
                    usernames=[names[0], names[400], names[800]],
                )
                self.assertEqual({"total": 1200, "pending": 0, "recorded": 3, "deduped": 1197}, stats)
                with database.read() as connection:
                    recorded = connection.execute(
                        "SELECT username_norm FROM task_mode_candidates WHERE state='recorded' ORDER BY username_norm"
                    ).fetchall()
                indexes = (399, 799, 1199) if reverse else (0, 400, 800)
                self.assertEqual([names[index] for index in indexes], [row[0] for row in recorded])
                self.assertEqual(before, self.evidence(database))

    def test_full_repair_of_independent_identities_has_linear_work(self):
        for recognition in (False, True):
            samples = []
            for count in (100, 1200):
                database, service, _ = self.prepare(count, separate=True, recognition=recognition)
                before = self.evidence(database)
                stats, steps = database.measure(lambda: service.reconcile_task_mode_candidates(
                    "owner", "task", "target", "followers",
                ))
                self.assertEqual({"total": count, "pending": 0, "recorded": count, "deduped": 0}, stats)
                self.assertEqual(before, self.evidence(database))
                samples.append(steps)
            self.assertLess(samples[1], samples[0] * 16 + 2000, (recognition, samples))

    def test_scoped_late_identity_does_not_scan_unrelated_earlier_candidates(self):
        for recognition in (False, True):
            samples = []
            for count in (100, 3000):
                database, service, names = self.prepare(count, separate=True, recognition=recognition)
                stats, steps = database.measure(lambda: service.reconcile_task_mode_candidates(
                    "owner", "task", "target", "followers", usernames=[names[0]],
                ))
                self.assertEqual({"total": count, "pending": count - 1, "recorded": 1, "deduped": 0}, stats)
                samples.append(steps)
            self.assertLessEqual(samples[1], samples[0] * 3 + 2000, (recognition, samples))

    def test_full_repair_with_one_or_zero_pending_does_not_scan_recorded_history(self):
        samples = []
        empty_samples = []
        for count in (1000, 20_000):
            database, service, names = self.prepare(count, separate=True)
            with database.write() as connection:
                connection.execute(
                    "UPDATE task_mode_candidates SET state='recorded',processed_at=?,updated_at=? "
                    "WHERE username_norm<>?", (STAMP, STAMP, names[0]),
                )
            stats, steps = database.measure(lambda: service.reconcile_task_mode_candidates(
                "owner", "task", "target", "followers",
            ))
            self.assertEqual({"total": count, "pending": 0, "recorded": count, "deduped": 0}, stats)
            empty_stats, empty_steps = database.measure(lambda: service.reconcile_task_mode_candidates(
                "owner", "task", "target", "followers",
            ))
            self.assertEqual(stats, empty_stats)
            samples.append(steps)
            empty_samples.append(empty_steps)
        self.assertLessEqual(samples[1], samples[0] * 3 + 2000, samples)
        self.assertLessEqual(empty_samples[1], empty_samples[0] * 3 + 2000, empty_samples)

    def test_existing_recorded_alias_takes_precedence_over_earlier_pending_alias(self):
        for recognition in (False, True):
            database, service, names = self.prepare(1200, recognition=recognition)
            service.finish_task_mode_candidate(
                "owner", "task", "target", "followers", names[0], state="recorded"
            )
            before = self.evidence(database)
            stats = service.reconcile_task_mode_candidates(
                "owner", "task", "target", "followers", usernames=[names[-1]]
            )
            self.assertEqual({"total": 1200, "pending": 0, "recorded": 1, "deduped": 1199}, stats)
            with database.read() as connection:
                recorded = connection.execute(
                    "SELECT username_norm FROM task_mode_candidates WHERE state='recorded'"
                ).fetchall()
            self.assertEqual([names[0]], [row[0] for row in recorded])
            self.assertEqual(before, self.evidence(database))

    def test_rank_zero_saved_identity_finishes_without_reopening_or_eligible_review(self):
        database, service, names = self.prepare(1200, recognition=True, reviewed=False)
        before = self.evidence(database)
        expected = {"total": 1200, "pending": 0, "recorded": 1, "deduped": 1199}
        self.assertEqual(expected, service.reconcile_task_mode_candidates(
            "owner", "task", "target", "followers", usernames=[names[0]],
        ))
        expected = {"total": 1200, "pending": 0, "recorded": 1, "deduped": 1199}
        self.assertEqual(expected, service.reconcile_task_mode_candidates(
            "owner", "task", "target", "followers",
        ))
        self.assertEqual(before, self.evidence(database))

    def test_appending_later_alias_does_not_steal_primary_recording(self):
        database, service, names = self.prepare(1)
        with database.write() as connection:
            connection.execute(
                "INSERT INTO instagram_username_aliases(account_id,username_norm,first_seen_at,last_seen_at) VALUES('identity','later.alias',?,?)",
                (STAMP, STAMP),
            )
        before = self.evidence(database)
        appended = service.append_task_mode_candidates(
            "owner", "task", "target", "followers", ["later.alias"]
        )
        self.assertEqual(0, appended["recorded"])
        self.assertEqual(1, appended["pending"])
        self.assertEqual(1, appended["deduped"])  # The later alias is already-known history.
        stats = service.reconcile_task_mode_candidates("owner", "task", "target", "followers")
        self.assertEqual({"total": 2, "pending": 0, "recorded": 1, "deduped": 1}, stats)
        with database.read() as connection:
            recorded = connection.execute("SELECT username_norm FROM task_mode_candidates WHERE state='recorded'").fetchall()
        self.assertEqual([names[0]], [row[0] for row in recorded])
        self.assertEqual(before, self.evidence(database))


if __name__ == "__main__":
    unittest.main()
