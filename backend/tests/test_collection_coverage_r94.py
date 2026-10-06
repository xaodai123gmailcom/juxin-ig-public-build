"""R94 reconciles the 432/326 report without changing collection/lease semantics."""
import json
import unittest

import test_split_completion_details_r56 as fixtures
from app.collection_coverage import describe_coverage
from app.split_completion_details import report_details


class CoverageMathTests(unittest.TestCase):
    def test_screenshot_gap_is_not_a_duplicate_count(self):
        progress = dict(source_total=432, discovered=326, processed=326,
                        saved=324, skipped_global_duplicates=2)
        result = describe_coverage(progress, finished=True)
        self.assertEqual(('gap', 106, 106, 0), tuple(result[k] for k in
            ('status', 'remaining_count', 'unobserved_count', 'pending_count')))
        self.assertEqual(2, progress['skipped_global_duplicates'])

    def test_pending_and_unseen_partition_the_gap(self):
        result = describe_coverage(dict(source_total=432, discovered=350, processed=326), finished=True)
        self.assertEqual(('pending', 106, 82, 24), tuple(result[k] for k in
            ('status', 'remaining_count', 'unobserved_count', 'pending_count')))

    def test_numeric_equality_does_not_prove_end(self):
        progress = dict(source_total=326, discovered=326, processed=326)
        self.assertEqual('in_progress', describe_coverage(progress)['status'])
        self.assertEqual('reconciled', describe_coverage(progress, finished=True)['status'])

    def test_unknown_and_invalid_counts_never_become_zero(self):
        for value in (None, True, -1, '432', 432.1):
            result = describe_coverage(dict(source_total=value, discovered=326, processed=326), finished=True)
            self.assertEqual('unverified', result['status'])
            self.assertIsNone(result['remaining_count'])
        result = describe_coverage(dict(source_total=432, processed=326), finished=True)
        self.assertIsNone(result['unobserved_count'])
        self.assertIsNone(result['pending_count'])

    def test_different_count_snapshots_do_not_claim_full_coverage(self):
        for progress in (dict(source_total=2, discovered=3, processed=3),
                         dict(source_total=4, discovered=2, processed=3)):
            self.assertEqual('count_mismatch', describe_coverage(progress, finished=True)['status'])

    def test_old_history_cannot_invent_mode_or_end_reason(self):
        old = report_details(json.dumps(dict(version=1, followers=432, following=100,
                                           processed_count=326, new_count=324, duplicate_count=2)))
        self.assertEqual({}, old['mode_coverage'])
        self.assertEqual(2, old['duplicate_count'])


class CoveragePersistenceTests(unittest.TestCase):
    setUp = fixtures.SplitCompletionDetailsR56Tests.setUp
    tearDown = fixtures.SplitCompletionDetailsR56Tests.tearDown
    checkpoint = fixtures.SplitCompletionDetailsR56Tests.checkpoint
    finish = fixtures.SplitCompletionDetailsR56Tests.finish
    items = fixtures.SplitCompletionDetailsR56Tests.items

    def gap(self):
        self.checkpoint('followers', dict(source_total=432, discovered=326,
            processed=326, saved=324, skipped_global_duplicates=2))

    def test_task_and_report_preserve_gap_per_mode_and_after_task_delete(self):
        self.gap()
        self.checkpoint('following', dict(source_total=10, discovered=10,
            processed=10, saved=8, skipped_global_duplicates=2))
        task = self.service.get_task(self.owner, self.task['id'])
        coverage = task['targets'][0]['mode_coverage']
        self.assertEqual('gap', coverage['followers']['status'])
        self.assertEqual('reconciled', coverage['following']['status'])
        self.finish()
        before = self.items()[0]
        self.assertEqual(106, before['mode_coverage']['followers']['remaining_count'])
        self.assertEqual(0, before['mode_coverage']['following']['remaining_count'])
        # Exercise retained history after a legacy import/cleanup removes task rows.
        with self.db.write() as c:
            c.execute('DELETE FROM tasks WHERE id=?', (self.task['id'],))
        self.db.initialize()
        self.assertEqual(before, self.items()[0])

    def test_report_is_frozen_not_replaced_by_later_checkpoint_or_read(self):
        self.gap()
        self.finish()
        before = self.items()[0]
        self.checkpoint('followers', dict(source_total=432, discovered=432,
            processed=432, saved=430, skipped_global_duplicates=2))
        self.assertEqual(before, self.items()[0])
        with self.db.read() as c:
            db_before = tuple(c.iterdump())
        self.items()
        with self.db.read() as c:
            self.assertEqual(db_before, tuple(c.iterdump()))

    def test_recovery_cursor_retains_end_but_explicit_false_wins(self):
        counters = dict(source_total=432, discovered=326, processed=326, saved=324,
                        skipped_global_duplicates=2)
        for cursor, expected in (({'resume_cursor': {'candidate_spool_complete': True,
                'candidate_spool_natural_end': True}}, True),
                ({'candidate_spool_complete': False, 'resume_cursor': {
                    'candidate_spool_complete': True, 'candidate_spool_natural_end': True}}, False)):
            self.service.upsert_checkpoint(self.owner, self.task['id'], self.target, mode='followers',
                stage='waiting_network', cursor=cursor, counters=counters, recoverable=True)
            task = self.service.get_task(self.owner, self.task['id'])
            value = task['targets'][0]['mode_coverage']['followers']
            self.assertEqual(expected, value['discovery_finished'])

    def test_live_candidate_counters_win_over_stale_completed_checkpoint(self):
        self.gap()
        self.service.append_task_mode_candidates(self.owner, self.task['id'], self.target,
                                                 'followers', ['pending.account'])
        value = self.service.get_task(self.owner, self.task['id'])['targets'][0]['mode_coverage']['followers']
        self.assertEqual('pending', value['status'])
        self.assertEqual(1, value['pending_count'])
        self.assertEqual(432, value['remaining_count'])


if __name__ == '__main__':
    unittest.main()
