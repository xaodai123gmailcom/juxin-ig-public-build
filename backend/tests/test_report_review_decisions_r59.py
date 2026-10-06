"""Human judgments persist independently of pagination and source execution records."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.errors import ValidationError
from app.work_reports import set_report_review_decision
from tests import test_split_review_report_r54, test_private_follow_review_r56


class ReportReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        # Business fixtures are dated September 24; wall-clock passage must not
        # invalidate the production seven-local-day rule these tests exercise.
        class FixtureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 24, 12, tzinfo=timezone.utc).astimezone(tz)
        clock = patch("app.work_reports.datetime", FixtureDateTime)
        clock.start()
        self.addCleanup(clock.stop)

    def test_split_decisions_count_all_rows_and_allow_correction(self):
        fixture = test_split_review_report_r54.SplitReviewReportR54Tests(); fixture.setUp()
        try:
            fixture.history('first.account', '2026-09-24T08:00:00Z')
            fixture.history('second.account', '2026-09-24T09:00:00Z')
            start, end = '2026-09-24T00:00:00+00:00', '2026-09-25T00:00:00+00:00'
            report = fixture.report(start, end, utc_offset_minutes=0, limit=1,
                                    now=datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
            ident = report['items'][0]['id']
            self.assertEqual({'passed': 0, 'failed': 0}, report['decision_counts'])
            self.assertEqual({'passed': 1, 'failed': 0}, set_report_review_decision(
                fixture.db, fixture.owner, 'split', ident, 'passed', start, end, 0)['decision_counts'])
            self.assertEqual({'passed': 0, 'failed': 1}, set_report_review_decision(
                fixture.db, fixture.owner, 'split', ident, 'failed', start, end, 0)['decision_counts'])
            self.assertEqual('failed', fixture.report(start, end, utc_offset_minutes=0, limit=1,
                now=datetime(2026, 9, 24, 12, tzinfo=timezone.utc))['items'][0]['review_decision'])
            with self.assertRaises(ValidationError):
                set_report_review_decision(fixture.db, fixture.owner, 'split', ident, 'passed',
                                           '2026-09-23T00:00:00+00:00', start, 0)
        finally:
            fixture.tearDown()

    def test_private_follow_is_owner_scoped(self):
        fixture = test_private_follow_review_r56.PrivateFollowReviewR56Tests(); fixture.setUp()
        try:
            fixture.follow('private.one')
            report = fixture.report('2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00')
            ident = report['items'][0]['id']
            with self.assertRaises(ValidationError):
                set_report_review_decision(fixture.db, fixture.other, 'private_follow', ident,
                    'passed', '2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00', 480)
            set_report_review_decision(fixture.db, fixture.owner, 'private_follow', ident,
                'passed', '2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00', 480)
            self.assertEqual({'passed': 1, 'failed': 0}, fixture.report(
                '2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00')['decision_counts'])
        finally:
            fixture.tearDown()
