"""Native cold-start release gate. Synthetic pages only; no Share or Like occurs.

Unlike embedded_posting_probe, neither test profile is opened before the real
manager/worker requests its connection. The Electron host serves isolated,
synthetic Instagram pages and cookies in only these fixture sessions.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.account_workspace import AccountWorkspace
from app.bitbrowser_api import BitBrowserClient
from app.database import Database
from app.embedded_browser import EmbeddedBrowser
from app.errors import ValidationError
from app.native_browser import BrowserHub
from app.posting_account_stats import save_account_snapshot
from app.posting_executor import PostingExecutor
from app.posting_workflow import PostingManager
from app.posting_workflow_selftest import SyntheticProvider
from app.service import CoreService
from app.studio import StudioManager
from app.studio_worker import StudioBrowser

USERNAME='fixture_own'
ACTOR='123456789'


class IdentityReady(Exception):pass


async def main():
    with tempfile.TemporaryDirectory(prefix='cold-task-native-') as directory:
        db=Database(Path(directory)/'fixture.sqlite3');db.initialize();service=CoreService(db)
        owner=service.register_user('offline-cold-task','synthetic fixture password')['id']
        native=EmbeddedBrowser(db,directory)
        hub=BrowserHub(native,BitBrowserClient('http://127.0.0.1:59999',''))
        accounts=AccountWorkspace(service,hub)
        profiles={}
        for kind in ('posting','nurture'):
            plan=accounts.save(owner,{'name':'OFFLINE COLD '+kind,'native':True})
            profiles[kind]=accounts.get(owner,plan['id'])['profile_id']
        assert not any(row['is_open'] for row in native.inventory()),'fixture must begin with all native windows closed'
        checks={'posting':False,'nurture':False}

        class ProbePosting(PostingExecutor):
            async def publish(self,job,asset,checkpoint,before_submit,confirmed):
                async def stop_before_share():
                    self.manager.assert_lease(job)
                    assert job['expected_actor_id']==ACTOR and job['expected_username']==USERNAME
                    assert self.worker.page.url.rstrip('/')=='https://www.instagram.com'
                    checks['posting']=True
                    raise ValidationError('Offline cold-start proof stops before Share')
                return await super().publish(job,asset,checkpoint,stop_before_share,confirmed)

        posting=PostingManager(service,hub,provider=SyntheticProvider(db),executor_factory=ProbePosting)
        posting.recover()
        ident=posting.generate(owner,{'request_id':'offline-cold-posting','theme':'fixture','caption':'exact offline caption','count':1})['job_ids'][0]
        await posting._prepare(posting.get(owner,ident))
        save_account_snapshot(db,owner,profiles['posting'],{'username':USERNAME,'posts_count':0,'followers_count':0,'following_count':0})
        await posting.command(owner,{'action':'assign','job_id':ident,'profile_id':profiles['posting'],'expected_username':USERNAME})
        job=posting.get(owner,ident)
        await posting.command(owner,{'action':'start','job_ids':[ident],'reviewed':[{k:job[k] for k in ('id','caption','asset_id','profile_id','expected_username','queue_revision')}]})
        await posting._execute(posting.get(owner,ident))
        row=posting.get(owner,ident)
        assert checks['posting'],row['message']
        assert row['status']=='failed' and not row['attempt_id'] and not row['submitted_at'] and not row['lease_token']
        assert not any(p['id']==profiles['posting'] and p['is_open'] for p in native.inventory())
        with db.read() as c:assert c.execute('SELECT COUNT(*) FROM posting_receipts').fetchone()[0]==0

        class ProbeNurture(StudioBrowser):
            async def nurture_step(self,step,counts,config):
                if checks['nurture']:return counts
                original=self.account_snapshot
                async def verified(data):
                    await original(data)
                    if data.get('username')!=USERNAME:return
                    assert data.get('instagram_user_id')==ACTOR
                    assert all(data.get(key)==0 for key in ('posts_count','followers_count','following_count'))
                    checks['nurture']=True
                    raise IdentityReady()
                self.account_snapshot=verified
                try:return await super().nurture_step(step,counts,config)
                except IdentityReady:return counts
                finally:self.account_snapshot=original

        studio=StudioManager(service,hub,posting_enabled=False);studio.recover()
        ident=(await studio.command(owner,{'action':'start','kind':'nurture','profile_ids':[profiles['nurture']],
            'request_id':'offline-cold-nurture','config':{'minutes':1,'rounds':1}}))['job_ids'][0]
        # Preserve validated duration/step configuration; ProbeNurture replaces
        # effects with no-ops after the real cold-start identity proof.
        studio.gates[ident]=asyncio.Event();studio.gates[ident].set()
        with patch('app.studio.StudioBrowser',ProbeNurture):await studio._execute(studio.get(owner,ident))
        result=studio.get(owner,ident)
        assert checks['nurture'],result['message']
        assert result['status']=='completed' and not result['inflight'],result
        assert not any(p['id']==profiles['nurture'] and p['is_open'] for p in native.inventory())
        with db.read() as c:assert c.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0]==0
        print('PASS native cold startup: posting and nurture auto-open, navigate, verify zero-count own account, and close; no Share/Like')


if __name__=='__main__':asyncio.run(main())
