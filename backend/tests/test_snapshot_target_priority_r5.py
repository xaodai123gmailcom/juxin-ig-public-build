"""Bounded target IDs retain the former task/detail page and truncation semantics."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.database import Database
from app.service import CoreService
from support.task_detail_reference import legacy_list_tasks_with_details

NOW='2026-10-01T00:00:00Z'


class SnapshotTargetPriorityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.db=Database(Path(tmp.name)/'page.db');self.db.initialize();self.svc=CoreService(self.db)
        with self.db.write() as c:
            for owner in ['owner','foreign']:
                c.execute('INSERT INTO app_users(id,username_norm,username_display,password_hash,created_at) VALUES(?,?,?,?,?)',(owner,owner,owner,'synthetic',NOW))
            for i,status in enumerate(['running','paused','completed','stopped','running']):
                owner='foreign' if i==4 else 'owner'
                c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES(?,?,?,?, '[\"followers\"]','{\"live_queue_enabled\":true}',?,?)",(f'task{i}',owner,f'Task{i}',status,NOW,NOW))
                for j in range(18):
                    status=['completed','running','waiting_network','failed','recoverable','pending','paused','stopped','completed'][j%9]
                    # Terminal cleanup bindings and target dismissals remain part
                    # of the same original selection rules, not silently removed.
                    window=f'window{j}' if status in ['running','waiting_network'] or j==0 else None
                    username=f'fb:legacy{i}' if j==17 else f'source{i}_{j}'
                    c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,current_window_id,source_profile_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(f't{i}-{j:02}',f'task{i}',username,username,j,status,window,'{"posts_count":10}',NOW,NOW))
                    if j%3==0:
                        c.execute('INSERT INTO task_target_list_dismissals(target_id,owner_user_id,task_id,dismissed_at) VALUES(?,?,?,?)',(f't{i}-{j:02}',owner,f'task{i}',NOW))
                for j,status in enumerate(['running','waiting_network','paused','selected','stopped']):
                    c.execute('INSERT INTO task_windows VALUES(?,?,?,?)',(f'task{i}',f'window{j}',j,status))
            c.execute('INSERT INTO task_list_dismissals VALUES(?,?,?)',('task3','owner',NOW))

    def equivalent(self, **kwargs):
        expected=legacy_list_tasks_with_details(self.svc,'owner',**kwargs)
        actual=self.svc.list_tasks_with_details('owner',**kwargs)
        self.assertEqual(expected,actual,kwargs)
        self.assertNotIn('task4', [row['id'] for row in actual])
        return actual

    def test_exact_legacy_output_for_pages_offsets_and_detail_budgets(self):
        for limit in [None,1,2,10]:
            for offset in [0,1,3]:
                for details in [None,1,3,10,100]:
                    with self.subTest(limit=limit,offset=offset,details=details):
                        self.equivalent(limit=limit,offset=offset,detail_limit=details,platform='instagram')

    def test_status_owner_dismissal_and_order_mutations_keep_exact_output(self):
        changes=[
            ("UPDATE task_targets SET status='running',current_window_id='new-window' WHERE id='t0-05'",()),
            ("UPDATE task_targets SET status='completed' WHERE id='t0-01'",()),
            ("UPDATE tasks SET status='completed' WHERE id='task0'",()),
            ("UPDATE tasks SET owner_user_id='foreign' WHERE id='task1'",()),
            ("DELETE FROM task_list_dismissals WHERE task_id='task3'",()),
            ("DELETE FROM task_target_list_dismissals WHERE target_id='t0-00'",()),
            ("UPDATE task_targets SET queue_order=100 WHERE id='t0-02'",()),
        ]
        for sql,params in changes:
            with self.db.write() as c:c.execute(sql,params)
            self.equivalent(limit=10,detail_limit=15)
        # Separate live-status retains every occupied window, including the
        # existing terminal cleanup behavior, independent of fair page budgets.
        live=self.svc.get_task_live_status('owner','task0')
        ids={row['id'] for row in live['targets']}
        self.assertTrue({'t0-00','t0-01','t0-02'} <= ids)

    def test_selected_ids_and_payloads_share_one_read_snapshot(self):
        expected=self.svc.list_tasks_with_details('owner',limit=1,detail_limit=5)
        original=self.db._connect;changed=[]
        def connect():
            c=original()
            def trace(sql):
                if 'WITH ranked_targets AS (SELECT value AS id' in sql and not changed:
                    changed.append(True)
                    writer=original()
                    try:
                        writer.execute("UPDATE task_targets SET status='completed' WHERE id='t0-01'")
                    finally:
                        writer.close()
            c.set_trace_callback(trace)
            return c
        with patch.object(self.db,'_connect',connect):
            actual=self.svc.list_tasks_with_details('owner',limit=1,detail_limit=5)
        self.assertTrue(changed)
        self.assertEqual(expected,actual)
        with self.db.read() as c:
            self.assertEqual('completed',c.execute("SELECT status FROM task_targets WHERE id='t0-01'").fetchone()[0])

    def test_priority_seek_and_truncation_work_do_not_grow_with_completed_history(self):
        with self.db.write() as c:
            c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES('long','owner','Long','running','[\"followers\"]','{}',?,?)",('2099', '2099'))
        original=self.db._connect
        work=[];statements=[]
        def measure():
            steps=[0]
            def connect():
                c=original();c.set_trace_callback(statements.append)
                c.set_progress_handler(lambda: steps.__setitem__(0,steps[0]+1) or 0,1)
                return c
            with patch.object(self.db,'_connect',connect):
                page=self.svc.list_tasks_with_details('owner',limit=1,detail_limit=10)
            self.assertEqual(10,len(page[0]['targets']));self.assertTrue(page[0]['targets_truncated'])
            return steps[0]
        for start,end in [(0,1000),(1000,20000)]:
            with self.db.write() as c:
                c.executemany("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES(?,'long',?,?,?,'completed',?,?)",((f'long{i:07}',f'long_source{i}',f'long_source{i}',i,NOW,NOW) for i in range(start,end)))
            work.append(measure())
        self.assertLess(max(work),15000,work)
        self.assertLessEqual(max(work),min(work)*1.05,work)
        selection=next(sql for sql in statements if 'json_group_array(id)' in sql)
        task_page=next(sql for sql in statements if 'SELECT task.* FROM tasks task' in sql)
        with self.db.read() as c:
            target_plan=[row[3] for row in c.execute('EXPLAIN QUERY PLAN '+selection)]
            self.assertTrue(any('idx_targets_task_snapshot_priority' in p for p in target_plan),target_plan)
            self.assertFalse(any('TEMP B-TREE' in p for p in target_plan),target_plan)
            task_plan=[row[3] for row in c.execute('EXPLAIN QUERY PLAN '+task_page)]
            self.assertTrue(any('idx_tasks_owner_snapshot_priority' in p for p in task_plan),task_plan)
            self.assertFalse(any('TEMP B-TREE' in p for p in task_plan),task_plan)
        self.assertFalse(any('COUNT(*)' in sql and 'FROM task_targets' in sql for sql in statements))


if __name__=='__main__':unittest.main()
