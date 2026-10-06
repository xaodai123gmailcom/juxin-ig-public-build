"""Restore user-opened manual pages once per signed-in owner, never task browsers."""
import threading
from .service import isoformat


def initialize_restore_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS account_window_open_state (
        plan_id TEXT PRIMARY KEY REFERENCES account_window_plans(id) ON DELETE CASCADE,
        owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        opened INTEGER NOT NULL CHECK(opened IN (0,1)), updated_at TEXT NOT NULL)''')
    # Upgrade existing accounts using the last explicit manual open/close event.
    c.execute('''INSERT OR IGNORE INTO account_window_open_state
        SELECT p.id,p.owner_user_id,CASE WHEN e.action LIKE '关闭窗口%' OR e.action LIKE '重置本窗口%' THEN 0 ELSE 1 END,e.created_at
        FROM account_window_plans p JOIN account_window_events e ON e.plan_id=p.id AND e.owner_user_id=p.owner_user_id
        WHERE p.archived=0 AND p.profile_id LIKE 'native:%' AND e.seq=(
          SELECT max(x.seq) FROM account_window_events x WHERE x.plan_id=p.id AND x.owner_user_id=p.owner_user_id
          AND (x.action LIKE '打开平台窗口%' OR x.action LIKE '导入 Cookie%' OR x.action LIKE '打开主页%'
            OR x.action LIKE '打开私信%' OR x.action LIKE '关闭窗口%' OR x.action LIKE '重置本窗口%'))''')
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(28,?)',(isoformat(),))


class AccountWindowRestore:
    def __init__(self,accounts):
        self.accounts=accounts;self.lock=threading.Lock();self.started=set();self.pending={};self.failures={}

    def remember(self,c,owner,plan,opened):
        c.execute('''INSERT INTO account_window_open_state VALUES(?,?,?,?)
            ON CONFLICT(plan_id) DO UPDATE SET opened=excluded.opened,updated_at=excluded.updated_at''',
            (plan,owner,int(opened),isoformat()))

    def start(self,owner):
        if not callable(getattr(getattr(self.accounts.bitbrowser,'native',None),'surface_show',None)):return
        with self.lock:
            if owner in self.started:return
            self.started.add(owner);self.pending[owner]=True
        threading.Thread(target=self.run,args=(owner,),daemon=True,name='account-window-restore').start()

    def run(self,owner):
        a=self.accounts;failed=[]
        try:
            with a.db.read() as c:
                rows=[dict(r) for r in c.execute('''SELECT p.id,p.profile_id FROM account_window_plans p
                    JOIN account_window_open_state s ON s.plan_id=p.id AND s.owner_user_id=p.owner_user_id
                    WHERE p.owner_user_id=? AND p.archived=0 AND s.opened=1 AND p.profile_id LIKE 'native:%' ORDER BY p.serial''',(owner,))]
            for row in rows:
                try:
                    # The ordinary command acquires the original task lease and
                    # rechecks remembered intent within that lease before opening.
                    if any(w['id']==row['profile_id'] and w.get('is_open') for w in a.bitbrowser.native.inventory()):continue
                    result=a.command(owner,{'id':row['id'],'action':'restore'})
                    if result.get('page_loaded') is False:failed.append(row['id'])
                except Exception:failed.append(row['id'])
        finally:
            with self.lock:self.pending[owner]=False;self.failures[owner]=failed

    def capture_before_shutdown(self):
        # Shutdown alone takes the short-lived acquisition fence around its
        # observation. Existing operations can finish, but no new manual/task
        # lease can appear and disappear entirely between inventory and commit.
        # Desktop I/O still never holds a SQLite write transaction.
        try:
            with self.accounts.db.browser_surface_lock:
                self._capture_manual_state()
        except Exception:
            # Explicit open/close records remain the crash-recovery fallback.
            pass

    def _capture_manual_state(self):
        # Task-created pages are never evidence of manual open intent.
        with self.lock:
            owners={o for o in self.started if not self.pending.get(o)}
            failed={o:set(self.failures.get(o,[])) for o in owners}
        if not owners:return
        with self.accounts.db.read() as c:
            occupied={r[0] for r in c.execute('SELECT profile_id FROM browser_operation_leases')}
            remembered=[dict(r) for r in c.execute("""SELECT p.id,p.profile_id,p.owner_user_id,s.updated_at
                FROM account_window_plans p JOIN account_window_open_state s
                ON s.plan_id=p.id AND s.owner_user_id=p.owner_user_id
                WHERE p.archived=0 AND p.profile_id LIKE 'native:%' AND s.opened=1""")
                if r['owner_user_id'] in owners]
        live={w['id'] for w in self.accounts.bitbrowser.native.inventory() if w.get('is_open')}
        with self.accounts.db.write() as c:
            occupied.update(r[0] for r in c.execute('SELECT profile_id FROM browser_operation_leases'))
            for row in remembered:
                owner=row['owner_user_id']
                if row['profile_id'] in live or row['profile_id'] in occupied or row['id'] in failed[owner]:continue
                c.execute("""UPDATE account_window_open_state SET opened=0,updated_at=?
                    WHERE plan_id=? AND owner_user_id=? AND opened=1 AND updated_at=?""",
                    (isoformat(),row['id'],owner,row['updated_at']))

    def status(self,owner):
        with self.lock:return {'restoring':self.pending.get(owner,False),'failed':list(self.failures.get(owner,[]))}
