"""Confirmed new-queue posts stay separate from legacy/manual posting totals."""
from pathlib import Path
import tempfile,json,unittest
from app.database import Database
from app.service import CoreService,isoformat
from app.posting_schema import initialize_posting_schema
from app.work_reports import work_report

class ConfirmedPostingReportR6Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'test.sqlite3');self.db.initialize();self.service=CoreService(self.db)
        self.owner=self.service.register_user('report_posts','fixture password only')['id']
        self.other=self.service.register_user('other_posts','fixture password only')['id']
        with self.db.write() as c:initialize_posting_schema(c)
    def add(self,ident,at,owner=None,verification='instagram_dialog'):
        with self.db.write() as c:c.execute('INSERT INTO posting_receipts VALUES(?,?,?,?,?,?,?,?)',(ident,owner or self.owner,'window','fixture_account','',at,at[:10],json.dumps({'verification':verification})))
    def report(self,start='2026-10-01T00:00:00Z',end='2026-10-02T00:00:00Z',summary=True):
        return work_report(self.db,self.owner,start,end,summary_only=summary)
    def test_only_native_receipts_count_and_legacy_csv_stays_separate(self):
        self.add('ok','2026-10-01T12:00:00+00:00');self.add('manual','2026-10-01T13:00:00+00:00',verification='manual')
        self.add('foreign','2026-10-01T12:00:00+00:00',owner=self.other)
        with self.db.write() as c:c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,status,result_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('legacy',self.owner,'legacy','posting','window','{}','completed',json.dumps({'published':1,'verification':'manual','confirmed_at':'2026-10-01T12:00:00Z'}),'2026-10-01','2026-10-01','2026-10-01'))
        summary=self.report();full=self.report(summary=False)
        self.assertEqual(1,summary['totals']['confirmed_posting']);self.assertEqual(1,full['totals']['confirmed_posting'])
        self.assertEqual(1,full['totals']['posting'])
        self.assertEqual(1,sum(row['confirmed_posting'] for row in full['rows']))
        self.assertEqual(1,sum(row['posting'] for row in full['rows']))
        self.assertEqual(0,summary['totals']['collection'])
    def test_offset_and_microsecond_boundaries_remain_exact(self):
        self.add('before','2026-10-01T23:59:59.999999+00:00')
        self.add('equal','2026-10-02T08:00:00+08:00')
        self.add('fraction','2026-10-02T00:00:00.000001+00:00')
        self.assertEqual(1,self.report()['totals']['confirmed_posting'])
        self.assertEqual(2,self.report('2026-10-02T00:00:00Z','2026-10-03T00:00:00Z')['totals']['confirmed_posting'])
        self.assertEqual(1,self.report('2026-10-02T00:00:00.000001Z','2026-10-02T00:00:00.000002Z')['totals']['confirmed_posting'])
    def test_no_cache_and_narrow_query_uses_owner_period_index(self):
        self.assertEqual(0,self.report()['totals']['confirmed_posting'])
        self.add('live','2026-10-01T10:00:00Z')
        self.assertEqual(1,self.report()['totals']['confirmed_posting'])
        with self.db.write() as c:c.execute('DELETE FROM posting_receipts')
        self.assertEqual(0,self.report()['totals']['confirmed_posting'])
        with self.db.read() as c:
            plan=' '.join(str(tuple(row)) for row in c.execute("EXPLAIN QUERY PLAN SELECT count(*) FROM posting_receipts WHERE owner_user_id=? AND julianday(confirmed_at)>=julianday(?) AND julianday(confirmed_at)<julianday(?)",(self.owner,'2026-10-01','2026-10-02')))
        self.assertIn('posting_receipts_report_period',plan)
