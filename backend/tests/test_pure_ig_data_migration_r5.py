"""Authorized FB retirement is bounded, atomic and preserves exact IG history."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import (Database, PURE_IG_MIGRATION_VERSION,
                          _purge_legacy_facebook_data, purge_legacy_facebook_data)

NOW = '2026-10-02T12:00:00+00:00'


def insert(c, table, **values):
    c.execute(f'INSERT INTO {table} ({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


class PureIGMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'mixed.sqlite3')
        self.db.initialize()
        with self.db.write() as c:
            c.execute('DELETE FROM schema_migrations WHERE version=?', (PURE_IG_MIGRATION_VERSION,))
            insert(c, 'app_users', id='owner', username_norm='owner', username_display='Owner', password_hash='synthetic-only', created_at=NOW)
            for platform, prefix, username, uid in (
                ('instagram', 'ig', 'same.person', '123456'),
                ('facebook', 'fb', 'fb:same.person', 'fbid:123456'),
            ):
                task, target, account = prefix+'-task', prefix+'-target', prefix+'-account'
                insert(c, 'tasks', id=task, owner_user_id='owner', name='Same Task', status='running',
                       modes_json='["followers"]', settings_json=json.dumps({'platform': platform}), created_at=NOW, updated_at=NOW)
                insert(c, 'task_targets', id=target, task_id=task, username_norm=username, username_display='Same Person',
                       queue_order=1, status='running', current_stage='profile', last_success_at=NOW,
                       source_profile_json=json.dumps({'platform': platform, 'evidence': prefix}), created_at=NOW, updated_at=NOW)
                insert(c, 'task_windows', task_id=task, profile_id='shared-window', queue_order=1)
                insert(c, 'task_list_dismissals', task_id=task, owner_user_id='owner', dismissed_at=NOW)
                insert(c, 'task_target_list_dismissals', target_id=target, task_id=task, owner_user_id='owner', dismissed_at=NOW)
                insert(c, 'task_checkpoints', id=prefix+'-checkpoint', task_id=task, target_id=target, mode='followers',
                       stage='profile', cursor_json='{}', counters_json='{}', updated_at=NOW)
                insert(c, 'task_mode_candidates', target_id=target, mode='followers', username_norm=username,
                       username_display='Same Person', discovery_order=1, discovered_at=NOW, updated_at=NOW)
                insert(c, 'task_source_rechecks', target_id=target, mode='followers', state='prepared', requested_at=NOW)
                insert(c, 'task_parent_reel_decisions', owner_user_id='owner', task_id=task, target_id=target,
                       reel_key='reel', like_selected=0, profile_id='shared-window', lease_token=prefix+'-old', decided_at=NOW)
                insert(c, 'task_automatic_completions', task_id=task, target_id=target, profile_id='shared-window', lease_token=prefix+'-old', updated_at=NOW)
                insert(c, 'instagram_accounts', id=account, instagram_user_id=uid, current_username_norm=username,
                       current_username_display='Same Person', first_seen_at=NOW, last_seen_at=NOW)
                insert(c, 'instagram_username_aliases', account_id=account, username_norm=username, first_seen_at=NOW, last_seen_at=NOW)
                insert(c, 'global_seen', account_id=account, sources_json='["followers"]', first_seen_at=NOW, last_seen_at=NOW)
                insert(c, 'global_identity_owners', account_id=account, owner_user_id='owner', first_seen_at=NOW, last_seen_at=NOW)
                insert(c, 'task_results', id=prefix+'-result', task_id=task, target_id=target, account_id=account,
                       sources_json='["followers"]', visibility='public', profile_json=json.dumps({'platform': platform, 'bio': 'retained '+prefix}),
                       screening_json='{}', created_at=NOW, updated_at=NOW)
                insert(c, 'task_result_duplicate_archive', original_result_id=prefix+'-archive', owner_user_id='owner',
                       task_id=task, target_id=target, account_id=account, sources_json='["followers"]', visibility='public',
                       profile_json=json.dumps({'platform': platform}), screening_json='{}', created_at=NOW, updated_at=NOW, archived_at=NOW)
                insert(c, 'workbench_identity_claims', account_id=account, claimed_by_user_id='owner', source='followers', source_target=username, claimed_at=NOW)
                insert(c, 'workbench_candidates', id=prefix+'-candidate', owner_user_id='owner', account_id=account,
                       visibility='public', status='approved', profile_json=json.dumps({'platform': platform}), created_at=NOW, updated_at=NOW)
                insert(c, 'workbench_review_decisions', id=prefix+'-decision', candidate_id=prefix+'-candidate', owner_user_id='owner',
                       decision='approved', visibility='public', profile_snapshot_json=json.dumps({'platform': platform}), screening_snapshot_json='{}', decided_at=NOW)
                insert(c, 'workbench_candidate_dismissals', id=prefix+'-dismissal', candidate_id=prefix+'-candidate', owner_user_id='owner', dismissed_at=NOW)
                insert(c, 'workbench_collection_exclusions', id=prefix+'-exclusion', account_id=account, owner_user_id='owner',
                       username_display='Same Person', reason_code='location', reason='retained', profile_snapshot_json=json.dumps({'platform': platform}), excluded_at=NOW)
                insert(c, 'task_target_recovery_controls', target_id=target, owner_user_id='owner', candidate_id=prefix+'-recovery',
                       username_norm=username, username_display='Same Person', state='dismissed', source_task_id=task, updated_at=NOW)
                insert(c, 'split_candidates', id=prefix+'-split', owner_user_id='owner', username_norm=username, username_display='Same Person',
                       candidate_kind='manual', source_task_id=task, source_target_id=target, profile_json=json.dumps({'platform': platform}), created_at=NOW, updated_at=NOW)
                insert(c, 'split_candidate_window_affinity', candidate_id=prefix+'-split', profile_id='shared-window', queue_order=1)
                insert(c, 'split_candidate_history', id=prefix+'-history', owner_user_id='owner', username_norm=username, username_display='Same Person',
                       source_task_id=task, source_target_id=target, source_status='completed', profile_json=json.dumps({'platform': platform}),
                       completed_at=NOW, created_at=NOW, updated_at=NOW)
                insert(c, 'split_completed_targets', target_id=target, owner_user_id='owner', username_norm=username, username_display='Same Person',
                       source_task_id=task, completed_at=NOW)
                insert(c, 'report_review_decisions', owner_user_id='owner', review_kind='split', record_id=target, decision='passed', reviewed_at=NOW)
                insert(c, 'event_log', owner_user_id='owner', entity_type='task', entity_id=task, event_type='task.created', payload_json='{}', created_at=NOW)
                insert(c, 'account_window_plans', id=prefix+'-plan', owner_user_id='owner', serial=1 if prefix=='ig' else 2,
                       name='Same Person', profile_id=prefix+'-profile', environment_json=json.dumps({'platform': platform}), created_at=NOW, updated_at=NOW)
                insert(c, 'account_window_events', owner_user_id='owner', plan_id=prefix+'-plan', name='Same Person', action='created', created_at=NOW)
                insert(c, 'account_window_open_state', plan_id=prefix+'-plan', owner_user_id='owner', opened=0, updated_at=NOW)
                insert(c, 'account_creation_batches', owner_user_id='owner', request_key=prefix+'-batch', fingerprint=prefix,
                       result_json=json.dumps({'id': prefix+'-plan', 'ids': [prefix+'-plan']}), created_at=NOW)
            insert(c, 'account_window_order', owner_user_id='owner', ids_json='["ig-plan","fb-plan"]')
            insert(c, 'native_browser_profiles', id='fb-profile', owner_user_id='owner', serial=1,
                   name='Facebook label is not proof of ownership', created_at=NOW, updated_at=NOW)
            insert(c, 'browser_operation_leases', profile_id='old-window', owner_user_id='owner', operation_type='collection',
                   entity_id='fb-task', lease_token='expired-fb', acquired_at=NOW, heartbeat_at=NOW, expires_at='2000-01-01')
        self.ig_tables = {
            'tasks': "id='ig-task'", 'task_targets': "id='ig-target'", 'task_windows': "task_id='ig-task'",
            'task_checkpoints': "task_id='ig-task'", 'task_results': "id='ig-result'",
            'task_result_duplicate_archive': "original_result_id='ig-archive'",
            'instagram_accounts': "id='ig-account'", 'instagram_username_aliases': "account_id='ig-account'",
            'global_seen': "account_id='ig-account'", 'global_identity_owners': "account_id='ig-account'",
            'workbench_identity_claims': "account_id='ig-account'", 'workbench_candidates': "id='ig-candidate'",
            'workbench_review_decisions': "id='ig-decision'", 'workbench_candidate_dismissals': "id='ig-dismissal'",
            'workbench_collection_exclusions': "id='ig-exclusion'", 'split_candidates': "id='ig-split'",
            'split_candidate_history': "id='ig-history'", 'split_completed_targets': "target_id='ig-target'",
            'split_admission_totals': "username_norm='same.person'", 'task_target_recovery_controls': "target_id='ig-target'",
            'account_window_plans': "id='ig-plan'", 'account_window_events': "plan_id='ig-plan'",
            'account_window_open_state': "plan_id='ig-plan'", 'account_creation_batches': "request_key='ig-batch'",
        }

    def tearDown(self):
        self.db.release_instance_lock()
        self.tmp.cleanup()

    def digest(self, selections=None):
        with self.db.read() as c:
            tables = selections or {r[0]: '1' for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            data = {table: sorted([tuple(r) for r in c.execute(f'SELECT * FROM {table} WHERE {where}')], key=repr)
                    for table, where in tables.items()}
            return hashlib.sha256(repr(data).encode()).hexdigest()

    def purge(self):
        with self.db.read() as c:
            return _purge_legacy_facebook_data(c)

    def assert_clean(self):
        with self.db.read() as c:
            self.assertFalse(list(c.execute('PRAGMA foreign_key_check')))
            self.assertEqual('ok', c.execute('PRAGMA quick_check').fetchone()[0])
            for table in ('tasks', 'task_targets', 'instagram_accounts', 'workbench_candidates', 'workbench_review_decisions',
                          'workbench_collection_exclusions', 'split_candidates', 'split_candidate_history'):
                self.assertFalse(list(c.execute(f"SELECT 1 FROM {table} WHERE id GLOB 'fb-*'")), table)
            self.assertEqual(1, c.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])
            self.assertEqual(0, c.execute("SELECT total_count FROM global_seen_platform_stats WHERE platform='facebook'").fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM task_mode_candidate_counters').fetchone()[0])
            self.assertEqual(1, c.execute('SELECT entries FROM event_log_usage').fetchone()[0])
            self.assertEqual('["ig-plan"]', c.execute('SELECT ids_json FROM account_window_order').fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM native_browser_profiles').fetchone()[0])

    def test_mixed_same_display_numeric_id_preserves_exact_ig_rows_and_foreign_keys(self):
        before = self.digest(self.ig_tables)
        self.assertTrue(self.purge())
        self.assert_clean()
        self.assertEqual(before, self.digest(self.ig_tables))
        self.assertFalse(list(Path(self.tmp.name).glob('*.bak')))

    def test_startup_and_repeated_restart_are_idempotent(self):
        before = self.digest(self.ig_tables)
        self.db.initialize()
        self.assert_clean()
        with self.db.read() as c:
            applied = c.execute('SELECT applied_at FROM schema_migrations WHERE version=?', (PURE_IG_MIGRATION_VERSION,)).fetchone()[0]
        self.assertFalse(self.purge())
        Database(self.db.path).initialize()
        self.assertEqual(before, self.digest(self.ig_tables))
        with self.db.read() as c:
            self.assertEqual(applied, c.execute('SELECT applied_at FROM schema_migrations WHERE version=?', (PURE_IG_MIGRATION_VERSION,)).fetchone()[0])

    def test_failure_after_deletes_rolls_back_rows_guards_and_marker(self):
        with self.db.write() as c:
            c.execute("CREATE TRIGGER fail_pure_ig BEFORE INSERT ON schema_migrations WHEN NEW.version=39 BEGIN SELECT RAISE(ABORT,'injected fault'); END")
        before = self.digest()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected fault'):
            self.purge()
        self.assertEqual(before, self.digest())
        self.assert_immutable()
        with self.db.write() as c:
            c.execute('DROP TRIGGER fail_pure_ig')
        self.assertTrue(self.purge())
        self.assert_clean()

    def assert_immutable(self):
        for table, where in (('workbench_review_decisions', "id='ig-decision'"),
                             ('workbench_candidate_dismissals', "id='ig-dismissal'"),
                             ('workbench_collection_exclusions', "id='ig-exclusion'")):
            with self.subTest(table=table), self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                with self.db.write() as c:
                    c.execute(f'DELETE FROM {table} WHERE {where}')

    def test_surviving_ig_ledgers_still_immutable(self):
        self.purge()
        self.assert_immutable()

    def test_live_lease_blocks_and_preserves_all_data_until_stopped(self):
        with self.db.write() as c:
            c.execute("UPDATE browser_operation_leases SET expires_at='2999-01-01'")
        before = self.digest()
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            self.purge()
        self.assertEqual(before, self.digest())
        with self.db.write() as c:
            c.execute("DELETE FROM browser_operation_leases WHERE lease_token='expired-fb'")
        self.purge()
        self.assert_clean()

    def test_active_fb_action_campaign_lease_is_not_preempted(self):
        with self.db.write() as c:
            insert(c, 'action_campaigns', id='active-action', owner_user_id='owner', operation='follow',
                   execution_type='manual', profile_id='active-action-window', interval_min_seconds=1,
                   interval_max_seconds=2, limit_count=2, status='running', created_at=NOW, updated_at=NOW)
            insert(c, 'action_targets', id='fb-live-action', campaign_id='active-action', username_norm='fb:recipient',
                   username_display='Same Person', queue_order=1, status='running', updated_at=NOW)
            insert(c, 'browser_operation_leases', profile_id='active-action-window', owner_user_id='owner', operation_type='action',
                   entity_id='active-action', lease_token='live-action', acquired_at=NOW, heartbeat_at=NOW, expires_at='2999-01-01')
        before = self.digest()
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            self.purge()
        self.assertEqual(before,self.digest())

    def test_active_process_and_same_instance_workers_cannot_be_preempted(self):
        self.db.acquire_instance_lock()
        before = self.digest()
        with self.assertRaisesRegex(RuntimeError, 'already using'):
            Database(self.db.path).initialize()
        self.db.live_browser_lease_tokens.add('occupied')
        with self.assertRaisesRegex(RuntimeError, 'browser work'):
            self.db.initialize()
        self.assertEqual(before, self.digest())
        self.db.live_browser_lease_tokens.clear()

    def test_restore_savepoint_never_commits_outer_transaction(self):
        before = self.digest()
        with self.db.read() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE tasks SET name='caller write' WHERE id='ig-task'")
            purge_legacy_facebook_data(c)
            self.assertTrue(c.in_transaction)
            c.rollback()
        self.assertEqual(before, self.digest())
        self.purge()
        with self.db.write() as c:
            insert(c, 'instagram_accounts', id='restored-fb', current_username_norm='fb:restored', current_username_display='restored', first_seen_at=NOW, last_seen_at=NOW)
            purge_legacy_facebook_data(c)
        with self.db.read() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM instagram_accounts WHERE id='restored-fb'").fetchone())

    def test_unprefixed_shared_identity_alias_collision_is_not_deleted(self):
        with self.db.write() as c:
            insert(c, 'instagram_username_aliases', account_id='fb-account', username_norm='historic.ig', first_seen_at=NOW, last_seen_at=NOW)
            insert(c, 'instagram_username_aliases', account_id='ig-account', username_norm='fb:accidental.alias', first_seen_at=NOW, last_seen_at=NOW)
            c.execute("UPDATE split_candidates SET queued_task_id='ig-task',queued_target_id='ig-target',username_norm='shared.unprefixed' WHERE id='fb-split'")
        self.purge()
        with self.db.read() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM instagram_accounts WHERE id='fb-account'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM global_seen WHERE account_id='fb-account'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM instagram_accounts WHERE id='ig-account'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM split_candidates WHERE id='fb-split'").fetchone())
            self.assertFalse(list(c.execute('PRAGMA foreign_key_check')))

    def test_orphan_detached_history_requires_positive_namespace(self):
        with self.db.write() as c:
            for username in ('fb:orphan', 'ambiguous.orphan'):
                insert(c, 'task_target_recovery_controls', target_id=username, owner_user_id='owner', candidate_id=username,
                       username_norm=username, username_display='Same Person', state='dismissed', source_task_id='missing-task', updated_at=NOW)
        self.purge()
        with self.db.read() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM task_target_recovery_controls WHERE target_id='fb:orphan'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM task_target_recovery_controls WHERE target_id='ambiguous.orphan'").fetchone())

    def test_invalid_settings_are_retained_without_json_error(self):
        with self.db.write() as c:
            c.execute("UPDATE tasks SET settings_json='{broken' WHERE id='fb-task'")
            c.execute("UPDATE task_targets SET username_norm='ambiguous' WHERE id='fb-target'")
        self.purge()
        with self.db.read() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM tasks WHERE id='fb-task'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM task_targets WHERE id='fb-target'").fetchone())

    def test_actions_reports_and_orphan_spool_keep_shared_ig_campaign(self):
        with self.db.write() as c:
            insert(c, 'action_campaigns', id='shared-campaign', owner_user_id='owner', operation='follow',
                   execution_type='manual', profile_id='shared-window', interval_min_seconds=1,
                   interval_max_seconds=2, limit_count=2, status='completed', created_at=NOW, updated_at=NOW)
            for prefix, username in (('ig','same.person'), ('fb','fb:same.person')):
                insert(c, 'action_targets', id=prefix+'-action', campaign_id='shared-campaign', username_norm=username,
                       username_display='Same Person', queue_order=1 if prefix=='ig' else 2, status='completed', updated_at=NOW)
                insert(c, 'action_attempts', id=prefix+'-attempt', campaign_id='shared-campaign', target_id=prefix+'-action',
                       attempt_number=1, status='succeeded', started_at=NOW, finished_at=NOW, details_json='{}')
                insert(c, 'action_success_ledger', owner_user_id='owner', operation='follow', username_norm=username,
                       username_display='Same Person', campaign_id='shared-campaign', target_id=prefix+'-action', attempt_id=prefix+'-attempt', completed_at=NOW)
                insert(c, 'private_follow_completions', owner_user_id='owner', username_norm=username,
                       username_display='Same Person', campaign_id='shared-campaign', target_id=prefix+'-action', attempt_id=prefix+'-attempt', completed_at=NOW)
                insert(c, 'action_dispatch_claims', owner_user_id='owner', operation='follow', username_norm=username,
                       campaign_id='shared-campaign', target_id=prefix+'-action', attempt_id=prefix+'-attempt', claimed_at=NOW)
                insert(c, 'report_review_decisions', owner_user_id='owner', review_kind='private_follow', record_id=prefix+'-attempt', decision='passed', reviewed_at=NOW)
                for record in ('target:'+prefix+'-target','history:'+prefix+'-history'):
                    insert(c, 'report_review_decisions', owner_user_id='owner', review_kind='split', record_id=record, decision='passed', reviewed_at=NOW)
            insert(c, 'task_mode_candidates', target_id='ig-target', mode='followers', username_norm='fb:contamination',
                   username_display='Same Person', discovery_order=2, discovered_at=NOW, updated_at=NOW)
        selections = dict(self.ig_tables, action_campaigns="id='shared-campaign'", action_targets="id='ig-action'",
            action_attempts="id='ig-attempt'", action_success_ledger="username_norm='same.person'",
            private_follow_completions="username_norm='same.person'", action_dispatch_claims="username_norm='same.person'")
        before = self.digest(selections)
        self.purge()
        self.assertEqual(before, self.digest(selections))
        with self.db.read() as c:
            self.assertFalse(list(c.execute("SELECT 1 FROM report_review_decisions WHERE record_id LIKE '%fb-%'")))
            self.assertEqual(1, c.execute('SELECT total FROM task_mode_candidate_counters').fetchone()[0])
            for table in ('action_targets','action_attempts','action_success_ledger','private_follow_completions','action_dispatch_claims'):
                self.assertEqual(1, c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        for table in ('action_success_ledger','private_follow_completions'):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                with self.db.write() as c:
                    c.execute(f'DELETE FROM {table}')

    def test_retired_profile_fence_contains_only_ids_and_preserves_shared_profiles(self):
        self.purge()
        with self.db.read() as c:
            self.assertEqual([('owner','fb-profile')], [tuple(r) for r in c.execute('SELECT * FROM retired_account_profiles')])
            self.assertEqual(['owner_user_id','profile_id'], [r[1] for r in c.execute('PRAGMA table_info(retired_account_profiles)')])
            self.assertIsNotNone(c.execute("SELECT 1 FROM native_browser_profiles WHERE id='fb-profile'").fetchone())
        # Reimport a FB plan sharing a profile with an archived IG plan. The
        # archived IG association remains evidence against exclusive FB ownership.
        with self.db.write() as c:
            insert(c, 'account_window_plans', id='restored-fb-plan', owner_user_id='owner', serial=3,
                   name='retired', profile_id='ig-profile', environment_json='{"platform":"facebook"}', archived=1, created_at=NOW, updated_at=NOW)
            purge_legacy_facebook_data(c)
            self.assertIsNone(c.execute("SELECT 1 FROM retired_account_profiles WHERE profile_id='ig-profile'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM account_window_plans WHERE id='ig-plan'").fetchone())

    def test_exact_cache_and_usage_counters_repair_after_fb_purge(self):
        with self.db.write() as c:
            for prefix, username in (('ig','retained.cache'), ('fb','fb:cache')):
                insert(c, 'instagram_accounts', id=prefix+'-cache-account', current_username_norm=username,
                       current_username_display='Same Person', first_seen_at=NOW, last_seen_at=NOW)
                insert(c, 'workbench_candidates', id=prefix+'-cache', owner_user_id='owner', account_id=prefix+'-cache-account',
                       visibility='private', review_cache_json=json.dumps({'preview': '北京'*30},ensure_ascii=False), created_at=NOW, updated_at=NOW)
            c.execute('UPDATE workbench_cache_usage SET pending_entries=99,pending_bytes=99')
            c.execute('UPDATE global_seen_stats SET total_count=99')
            c.execute('UPDATE event_log_usage SET entries=99,bytes=99')
            c.execute('UPDATE task_mode_candidate_counters SET total=99,pending=99')
        self.purge()
        with self.db.read() as c:
            expected = c.execute("SELECT length(CAST(review_cache_json AS BLOB))-2 FROM workbench_candidates WHERE id='ig-cache'").fetchone()[0]
            self.assertEqual((1,expected,0,0), tuple(c.execute('SELECT pending_entries,pending_bytes,terminal_entries,terminal_bytes FROM workbench_cache_usage').fetchone()))
            self.assertEqual((1,1), tuple(c.execute('SELECT total,pending FROM task_mode_candidate_counters').fetchone()))
            self.assertEqual(1,c.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])

    def seed_false_hover(self, prefix):
        username = 'fb:false.hover' if prefix=='fb' else 'false.hover'
        with self.db.write() as c:
            insert(c, 'instagram_accounts', id=prefix+'-hover', current_username_norm=username,
                   current_username_display=username, first_seen_at=NOW, last_seen_at=NOW)
            insert(c, 'global_seen', account_id=prefix+'-hover', sources_json='["followers"]', first_seen_at=NOW, last_seen_at=NOW)
            insert(c, 'workbench_collection_exclusions', id=prefix+'-hover-exclusion', account_id=prefix+'-hover', owner_user_id='owner',
                   username_display=username, reason_code='hover_preview_unavailable', reason='old false hover',
                   profile_snapshot_json='{"page_read_reason":"hover_card_missing_or_incomplete"}', excluded_at=NOW)

    def test_fb_purge_precedes_legacy_hover_backup_and_archive(self):
        self.seed_false_hover('fb')
        self.db.initialize()
        self.assertFalse(list(Path(self.tmp.name).glob('*.bak')))
        with self.db.read() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM hover_r64_repair_archive WHERE account_id='fb-hover'").fetchone())

    def test_unrelated_ig_hover_repair_backup_contains_no_purged_fb_rows(self):
        self.seed_false_hover('ig')
        self.db.initialize()
        backups = list(Path(self.tmp.name).glob('*.bak'))
        self.assertEqual(1,len(backups))
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM instagram_accounts WHERE current_username_norm GLOB 'fb:*'").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM tasks WHERE json_extract(settings_json,'$.platform')='facebook'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM instagram_accounts WHERE id='ig-hover'").fetchone())

    def test_invalid_receipts_and_ordering_arrays_are_preserved(self):
        invalid = ([None], ['fb-plan',None], ['fb-plan',1], ['fb-plan',{}], ['fb-plan',[]],
                   ['fb-plan',True], {'0':'fb-plan'}, None, 'fb-plan')
        with self.db.write() as c:
            for number, ids in enumerate(invalid):
                insert(c, 'account_creation_batches', owner_user_id='owner', request_key='invalid-'+str(number),
                       fingerprint='invalid', result_json=json.dumps({'ids':ids}), created_at=NOW)
            c.execute("UPDATE account_window_order SET ids_json=?", (json.dumps(['fb-plan',None]),))
        before = self.digest({'account_creation_batches':"request_key GLOB 'invalid-*'",'account_window_order':'1'})
        self.purge()
        self.assertEqual(before,self.digest({'account_creation_batches':"request_key GLOB 'invalid-*'",'account_window_order':'1'}))

    def test_typed_lease_id_collision_does_not_delete_ig_action_lease(self):
        with self.db.write() as c:
            insert(c, 'action_campaigns', id='fb-task', owner_user_id='owner', operation='follow',
                   execution_type='manual', profile_id='ig-action-window', interval_min_seconds=1,
                   interval_max_seconds=2, limit_count=2, status='completed', created_at=NOW, updated_at=NOW)
            insert(c, 'browser_operation_leases', profile_id='ig-action-window', owner_user_id='owner', operation_type='action',
                   entity_id='fb-task', lease_token='retained-ig-action', acquired_at=NOW, heartbeat_at=NOW, expires_at='2000-01-01')
            insert(c, 'app_users', id='other', username_norm='other', username_display='Other', password_hash='synthetic-only', created_at=NOW)
            insert(c, 'browser_operation_leases', profile_id='other-window', owner_user_id='other', operation_type='collection',
                   entity_id='fb-task', lease_token='retained-other-owner', acquired_at=NOW, heartbeat_at=NOW, expires_at='2000-01-01')
        before = self.digest({'browser_operation_leases':"lease_token GLOB 'retained-*'",'action_campaigns':'1'})
        self.purge()
        self.assertEqual(before,self.digest({'browser_operation_leases':"lease_token GLOB 'retained-*'",'action_campaigns':'1'}))

    def test_event_target_ids_are_qualified_by_entity_type(self):
        with self.db.write() as c:
            insert(c, 'action_campaigns', id='ig-collision-campaign', owner_user_id='owner', operation='follow',
                   execution_type='manual', profile_id='ig-action-window', interval_min_seconds=1,
                   interval_max_seconds=2, limit_count=2, status='completed', created_at=NOW, updated_at=NOW)
            insert(c, 'action_targets', id='fb-target', campaign_id='ig-collision-campaign', username_norm='ig.recipient',
                   username_display='IG person', queue_order=1, status='completed', updated_at=NOW)
            insert(c, 'event_log', owner_user_id='owner', entity_type='action_target', entity_id='fb-target', event_type='action.completed',
                   payload_json='{"platform":"instagram","target_id":"fb-target"}', created_at=NOW)
        before=self.digest({'event_log':"entity_type='action_target'",'action_targets':"id='fb-target'"})
        self.purge()
        self.assertEqual(before,self.digest({'event_log':"entity_type='action_target'",'action_targets':"id='fb-target'"}))

    def test_existing_external_backup_is_untouched(self):
        backup = Path(self.tmp.name)/'user-export.bak'
        payload = b'preexisting user export is outside this migration'
        backup.write_bytes(payload)
        self.purge()
        self.assertEqual(payload,backup.read_bytes())
        self.assertEqual([backup],list(Path(self.tmp.name).glob('*.bak')))

    def test_process_death_mid_delete_recovers_whole_transaction(self):
        before = self.digest()
        program = '''import os,sqlite3,sys
from app.database import Database,purge_legacy_facebook_data
c=Database(sys.argv[1])._connect()
c.set_trace_callback(lambda sql: os._exit(77) if sql.startswith('DELETE FROM instagram_accounts') else None)
purge_legacy_facebook_data(c)
'''
        env = dict(__import__('os').environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        result = subprocess.run([sys.executable, '-c', program, str(self.db.path)], env=env, timeout=30)
        self.assertEqual(77, result.returncode)
        self.assertEqual(before, self.digest())
        self.assert_immutable()
        self.purge()
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
