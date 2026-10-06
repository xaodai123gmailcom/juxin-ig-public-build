"""Merged recovery regression, using authenticated routes and disposable SQLite.

A queued post and an archived paused collection both block the same historical
nurture hold. Neither local recovery action may clear the remaining blocker or
substitute for affirmative closed-profile evidence. No browser/network/submit
operation is permitted by the shared offline fixture.
"""
import json
import unittest

from app.service import isoformat
import test_posting_withdraw_api_r63 as fixtures


class CombinedRecoveryR64Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PostingWithdrawApiR63Tests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        fixture = self.fixture
        service = fixture.app.state.service
        owner = fixture.users['owner']
        task = service.create_task(
            owner, name='Merged hidden collection', modes=['followers'],
            targets=['merged_fixture_source'], window_ids=[fixture.PROFILE], settings={},
        )
        self.task_id = task['id']
        target_id = task['targets'][0]['id']
        service.upsert_checkpoint(
            owner, self.task_id, target_id, mode='followers', stage='completed',
            cursor={'position':17}, counters={'saved':1},
        )
        service.record_result(
            owner, self.task_id, target_id, username='merged_fixture_result',
            instagram_user_id='merged-fixture-identity', source_mode='followers',
            visibility='public', profile={'followers_count':4}, screening={}, qualified=False,
        )
        with fixture.db.write() as connection:
            connection.execute("UPDATE tasks SET status='paused' WHERE id=?", (self.task_id,))
            connection.execute(
                "UPDATE task_targets SET status='completed', current_window_id=?, "
                "current_stage='completed_archived' WHERE task_id=?",
                (fixture.PROFILE, self.task_id),
            )
            connection.execute('INSERT INTO task_list_dismissals VALUES(?,?,?)',
                               (self.task_id, owner, isoformat()))
        self.original_review = fixture.reviewed(fixture.row('posting_jobs', fixture.POST))
        self.collection_history = self.retained_collection_history()
        for table in ('task_checkpoints', 'task_results', 'global_seen', 'global_identity_owners'):
            self.assertTrue(self.collection_history[table], table+' must contain retained evidence')

    def tearDown(self):
        self.fixture.tearDown()

    def retained_collection_history(self):
        tables = ('task_targets', 'task_checkpoints', 'task_results', 'task_list_dismissals',
                  'task_mode_candidates', 'instagram_accounts', 'instagram_username_aliases',
                  'global_seen', 'global_identity_owners', 'global_seen_stats',
                  'global_seen_platform_stats')
        with self.fixture.db.read() as connection:
            return {table:[tuple(row) for row in connection.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in tables}

    def studio_command(self, action, **fields):
        fixture = self.fixture
        return fixture.client.post('/api/studio/command', headers=fixture.headers['owner'],
                                   json={'action':action, 'job_id':fixture.NURTURE, **fields})

    def locate(self):
        response = self.studio_command('locate_cleanup_collection')
        self.assertEqual(200, response.status_code, response.text)
        blocker = response.json()['blocker']
        self.assertEqual(self.task_id, blocker['task_id'])
        self.assertTrue(blocker['dismissed'])
        self.assertTrue(blocker['can_stop'])
        return blocker

    def stop_collection(self, blocker):
        response = self.studio_command('stop_cleanup_collection',
                                       task_id=blocker['task_id'], version=blocker['version'])
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['cleanup_pending'])
        self.assertEqual('stopped', response.json()['status'])
        self.assertEqual(self.collection_history, self.retained_collection_history())

    def assert_hold_preserved(self):
        fixture = self.fixture
        result = json.loads(fixture.row('studio_jobs', fixture.NURTURE)['result_json'])
        self.assertTrue(result['window_hold'])
        self.assertEqual(fixture.historical_result['counts'], result['counts'])
        fixture.assert_preserved()
        self.assertEqual(self.collection_history, self.retained_collection_history())

    def assert_cleanup_blocked(self):
        response = self.fixture.cleanup()
        self.assertEqual(409, response.status_code, response.text)
        self.assert_hold_preserved()

    def finish_cleanup(self):
        fixture = self.fixture
        response = fixture.cleanup()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['cleanup_reconciled'])
        result = json.loads(fixture.row('studio_jobs', fixture.NURTURE)['result_json'])
        self.assertFalse(result['window_hold'])
        self.assertEqual(fixture.historical_result['counts'], result['counts'])
        self.assertGreater(fixture.provider.proofs_completed, 0)
        fixture.assert_preserved()
        self.assertEqual(self.collection_history, self.retained_collection_history())

    def test_withdraw_then_exact_stop_requires_closed_proof_and_fresh_review(self):
        fixture = self.fixture
        blocker = self.locate()
        self.assert_cleanup_blocked()
        fixture.withdraw()
        self.assert_cleanup_blocked()  # Archived paused collection still blocks.
        self.stop_collection(blocker)
        self.assert_hold_preserved()  # Neither local transition clears the hold.
        self.finish_cleanup()
        response = fixture.command({'action':'assign', 'job_id':fixture.POST,
                                    'profile_id':fixture.PROFILE, 'expected_username':fixture.USERNAME})
        self.assertEqual(200, response.status_code, response.text)
        stale = fixture.command({'action':'start', 'job_ids':[fixture.POST],
                                 'reviewed':[self.original_review]})
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual('ready', fixture.row('posting_jobs', fixture.POST)['status'])
        current = fixture.row('posting_jobs', fixture.POST)
        response = fixture.command({'action':'start', 'job_ids':[fixture.POST],
                                    'reviewed':[fixture.reviewed(current)]})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('queued', fixture.row('posting_jobs', fixture.POST)['status'])
        fixture.assert_preserved()

    def test_exact_stop_then_withdraw_keeps_remaining_posting_blocker(self):
        fixture = self.fixture
        self.assert_cleanup_blocked()
        self.stop_collection(self.locate())
        self.assert_cleanup_blocked()  # Queued post still owns its assignment.
        fixture.withdraw()
        self.assert_hold_preserved()
        self.finish_cleanup()
        self.assertEqual('ready', fixture.row('posting_jobs', fixture.POST)['status'])
        self.assertEqual('', fixture.row('posting_jobs', fixture.POST)['profile_id'])


if __name__ == '__main__':
    unittest.main()
