"""Installed offline posting proof: real queue/service/storage, synthetic effects.

No Pexels call, website, account, credential, or browser is used. Fabricated JPEG
responses exercise production reservation/download/hash/cleanup; a synthetic
publisher uses the real submit/receipt callbacks and production positive-close
fence. This is deliberately not evidence of live Instagram publication.
"""
from __future__ import annotations
import asyncio
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch
from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .service import CoreService, isoformat
from .posting_account_stats import save_account_snapshot
from .posting_executor import PostingExecutor
from .posting_pexels import PexelsProvider
from .posting_workflow import PostingManager

PROOF_PREFIX = 'POSTING_WORKFLOW_SELFTEST=PASS '
CAPTION = '  原始文案\nOffline exact caption, no rewriting  '


def require(value, message):
    if not value:raise RuntimeError('Posting workflow installed self-test: ' + message)


def rows(database, table):
    with database.read() as c:return [dict(row) for row in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]


class SyntheticProvider(PexelsProvider):
    """Only search results and HTTP bytes are fake; all durable file paths are real."""
    def __init__(self, database):
        super().__init__(database, lambda:'fixture-not-a-real-key', opener=self)
        self.sequence=0;self.payloads={};self.override=None;self.opens=[]
    def search(self, query, page=1):
        from PIL import Image
        self.sequence+=1;ident=str(self.sequence)
        url='https://images.pexels.com/photos/'+ident+'/offline.jpeg'
        if self.override is None:
            data=io.BytesIO();Image.new('RGB',(320,320),(self.sequence*31%255,self.sequence*47%255,self.sequence*61%255)).save(data,'JPEG')
            raw=data.getvalue()
        else:raw=self.override;self.override=None
        self.payloads[url]=raw
        return [{'provider_id':ident,'source_url':'https://www.pexels.com/photo/offline-'+ident+'/',
                 'download_url':url,'photographer':'Synthetic fixture only','photographer_url':''}]
    def open(self, request, *, timeout):
        require(request.full_url in self.payloads, 'synthetic material attempted an unknown URL')
        require(not request.has_header('Authorization'), 'media download attached a credential')
        require(timeout==25, 'material download lost its bounded deadline')
        self.opens.append(request.full_url)
        return io.BytesIO(self.payloads[request.full_url])


class Fixture:
    def __init__(self, directory, name):
        self.path=directory/(name+'.sqlite3');self.db=Database(self.path);self.db.initialize()
        self.service=CoreService(self.db)
        self.owner=self.service.register_user('offline-'+name,'isolated fixture password only')['id']
        self.other=self.service.register_user('other-'+name,'isolated fixture password only')['id']
        self.mode='success';self.close_reply=True;self.published=[];self.closed=[];self.disconnected=[];self.tokens={}
        self.provider=SyntheticProvider(self.db)
        self.manager=PostingManager(self.service,self,provider=self.provider,executor_factory=self.executor)
        self.manager.recover()
    def owned(self, profile):
        leases=[r for r in rows(self.db,'browser_operation_leases') if r['profile_id']==profile]
        require(len(leases)==1 and leases[0]['operation_type']=='posting','operation lacks exact posting lease')
        lease=leases[0]
        job=self.manager.get(lease['owner_user_id'],lease['entity_id'])
        require(job['lease_token']==lease['lease_token'],'job and shared lease generation differ')
        require(self.tokens.setdefault(profile,lease['lease_token'])==lease['lease_token'],'same execution changed lease generation')
        return job
    def close_profile(self, profile):
        job=self.owned(profile)
        require(profile in self.disconnected,'provider close preceded owned executor disconnect')
        if job['status']=='cleanup_pending':
            asset=self.manager._asset(job['asset_id'])
            require(asset['path']=='' and Path(asset['recovery_path']).is_file(),'success close preceded recoverable material cleanup')
            require(len([r for r in rows(self.db,'posting_receipts') if r['job_id']==job['id']])==1,'success close preceded exactly one durable receipt')
        self.closed.append((profile,self.close_reply))
        return {'closed':self.close_reply}
    def executor(self, manager):
        fixture=self
        class SyntheticExecutor(PostingExecutor):
            async def publish(self, job, asset, checkpoint, before_submit, confirmed):
                require(job['caption']==CAPTION,'publisher received rewritten caption')
                require(hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest()==asset['render_sha256'],'reviewed JPEG bytes do not match durable receipt')
                await checkpoint();fixture.owned(job['profile_id'])
                await before_submit()
                current=manager.get(job['owner_user_id'],job['id'])
                require(current['status']=='submitting' and current['attempt_id'] and current['submitted_at'],
                        'synthetic share preceded durable submit-once boundary')
                fixture.published.append(job['id'])
                if fixture.mode=='unknown':raise TimeoutError('Synthetic share result is unknown')
                result={'published':1,'verification':'instagram_dialog','confirmed_at':isoformat(),
                        'post_url':'https://www.instagram.com/p/OFFLINE_FIXTURE/'}
                await confirmed(result);await confirmed(result)
                return result
            async def disconnect(self):
                with manager.db.read() as c:
                    held=c.execute('SELECT profile_id FROM browser_operation_leases WHERE operation_type=\'posting\'').fetchall()
                fixture.disconnected.extend(row[0] for row in held)
                await super().disconnect()
        return SyntheticExecutor(manager)
    async def generate(self, count=1, *, owner=None, request='offline-request-0001'):
        return (await self.manager.command(owner or self.owner,{'action':'generate','request_id':request,
            'provider':'pexels','theme':'synthetic forest','caption':CAPTION,'count':count}))['job_ids']
    async def prepared(self, count=1, *, request='offline-request-0001'):
        ids=await self.generate(count,request=request)
        for number,ident in enumerate(ids):
            await self.manager._prepare(self.manager.get(self.owner,ident))
            require(self.manager.get(self.owner,ident)['status']=='ready','synthetic material was not prepared by production provider')
            profile='offline-window-'+str(number)
            save_account_snapshot(self.db,self.owner,profile,{'username':'offline_own_'+str(number),'posts_count':0,'followers_count':10,'following_count':4})
            await self.manager.command(self.owner,{'action':'assign','job_id':ident,'profile_id':profile,'expected_username':'offline_own_'+str(number)})
        return ids
    def start_body(self, ids):
        return {'action':'start','job_ids':ids,'reviewed':[
            {key:self.manager.get(self.owner,ident)[key] for key in ('id','caption','asset_id','profile_id','expected_username','queue_revision')} for ident in ids]}
    async def execute(self, ident):
        await self.manager.command(self.owner,self.start_body([ident]))
        await self.manager._execute(self.manager.get(self.owner,ident))
    def reopen(self):
        self.db=Database(self.path);self.db.initialize();self.service=CoreService(self.db)
        self.service.recover_interrupted_operations()
        self.provider=SyntheticProvider(self.db)
        self.manager=PostingManager(self.service,self,provider=self.provider,executor_factory=self.executor)
        self.manager.recover()


async def admission_case(directory):
    f=Fixture(directory,'admission')
    f.manager.provider=PexelsProvider(f.db,lambda:'')
    try:await f.generate()
    except ValidationError:pass
    else:raise RuntimeError('Unconfigured provider admitted a job')
    require(not rows(f.db,'posting_jobs') and not rows(f.db,'posting_assets'),'unconfigured state fabricated jobs/assets')
    f.manager.provider=f.provider
    ids=await f.prepared(2)
    require(await f.generate(2)==ids,'same generation request duplicated its batch')
    require(all(f.manager.get(f.owner,i)['caption']==CAPTION for i in ids),'generation rewrote exact caption')
    for field in ('caption','asset_id','profile_id','expected_username'):
        body=f.start_body(ids);body['reviewed'][1][field]='changed-after-review'
        try:await f.manager.command(f.owner,body)
        except ConflictError:pass
        else:raise RuntimeError('Changed review tuple was admitted: '+field)
        require(all(f.manager.get(f.owner,i)['status']=='ready' for i in ids),'review rejection partially queued batch')
    try:await f.manager.command(f.owner,{'action':'assign','job_id':ids[0],'profile_id':'unverified-window','expected_username':'offline_own_0'})
    except ValidationError:pass
    else:raise RuntimeError('Unverified account assignment was admitted')
    try:f.manager.get(f.other,ids[0])
    except NotFoundError:pass
    else:raise RuntimeError('Another owner read a posting task')
    require(f.manager.snapshot(f.other)['jobs']==[] and f.manager.snapshot(f.other)['totals']['success']==0,'posting snapshot crossed owners')
    blocker=f.service.acquire_browser_lease(f.owner,'offline-window-1',operation_type='studio',entity_id='sibling',ttl_seconds=600)
    try:
        try:await f.manager.command(f.owner,f.start_body(ids))
        except ConflictError:pass
        else:raise RuntimeError('Occupied selected window was admitted')
        require(all(f.manager.get(f.owner,i)['status']=='ready' for i in ids),'blocked batch partially queued')
        require(rows(f.db,'browser_operation_leases')[0]['lease_token']==blocker,'admission altered sibling lease')
    finally:f.service.release_browser_lease('offline-window-1',blocker)
    return {'verified':True,'unconfigured_no_fake_jobs':True,'exact_caption_preserved':True,
            'idempotent_generation':True,'reviewed_tuple_fields_checked':4,'reviewed_batch_atomic':True,
            'verified_account_assignment_required':True,'blocked_batch_atomic':True,'foreign_lease_untouched':True,'owner_isolation':True}


def assert_posting_report(f, expected):
    from .work_reports import work_report
    args=('2000-01-01T00:00:00+00:00','2100-01-01T00:00:00+00:00')
    for summary in (True,False):
        result=work_report(f.db,f.owner,*args,'instagram',summary_only=summary)
        require(result['totals']['confirmed_posting']==expected,'receipt-derived posting report differs from durable success count')
        require(all(result['totals'][key]==0 for key in ('collection','follow','split','added')),
                'posting receipt changed unrelated report metrics')
        other=work_report(f.db,f.other,*args,'instagram',summary_only=summary)
        require(other['totals']['confirmed_posting']==0,'posting report crossed owner boundary')


async def success_case(directory):
    f=Fixture(directory,'success');ident=(await f.prepared())[0]
    await f.execute(ident)
    job=f.manager.get(f.owner,ident);asset=f.manager._asset(job['asset_id']);receipts=rows(f.db,'posting_receipts')
    require(job['status']=='completed' and job['hidden_at'] and not job['lease_token'],'success did not retire card and lease')
    require(len(receipts)==1 and f.published==[ident],'double confirmation created another receipt/share')
    require(asset['state']=='used' and asset['path']=='' and Path(asset['recovery_path']).is_file(),'used material was deleted or lost registry')
    require(f.closed==[('offline-window-0',True)] and not rows(f.db,'browser_operation_leases'),'completed close/release missing')
    require(f.manager.snapshot(f.owner)['jobs']==[] and f.manager.snapshot(f.owner)['totals']['success']==1,'completed card or success count is wrong')
    assert_posting_report(f,1)
    f.reopen()
    require(rows(f.db,'posting_receipts')==receipts and f.manager.get(f.owner,ident)['status']=='completed','success receipt/state lost after restart')
    require(f.manager.snapshot(f.owner)['jobs']==[],'completed card returned after restart')
    try:await f.manager.command(f.owner,f.start_body([ident]))
    except ConflictError:pass
    else:raise RuntimeError('Completed posting was admitted for another share')
    assert_posting_report(f,1)
    return {'verified':True,'synthetic_publish_calls':1,'receipts':1,'double_confirmation_counted_once':True,
            'submit_fence_before_share':True,'recoverable_material_cleanup':True,'positive_close_before_release':True,
            'completed_card_removed':True,'completed_job_cannot_replay':True,'database_reopen_persistence':True,
            'confirmed_posting_report':1,'report_owner_isolation':True}


async def unknown_case(directory):
    f=Fixture(directory,'unknown');ident=(await f.prepared())[0];f.mode='unknown'
    await f.execute(ident);job=f.manager.get(f.owner,ident);token=job['lease_token']
    require(job['status']=='needs_review' and token and not rows(f.db,'posting_receipts'),'unknown share was counted or lost its hold')
    require(not f.closed and f.manager.snapshot(f.owner)['totals']['failed']==0,'unknown result became failure or closed automatically')
    with f.db.write() as c:
        c.execute("UPDATE posting_jobs SET status='submitting' WHERE id=?",(ident,))
        c.execute("UPDATE browser_operation_leases SET expires_at='2000-01-01T00:00:00+00:00',heartbeat_at='2000-01-01T00:00:00+00:00'")
    f.reopen()
    require(f.manager.get(f.owner,ident)['status']=='needs_review' and f.manager.get(f.owner,ident)['lease_token']==token,'restart did not fence interrupted submit')
    require(rows(f.db,'browser_operation_leases')[0]['lease_token']==token,'generic startup lost expired posting hold')
    try:f.service.acquire_browser_lease(f.owner,'offline-window-0',operation_type='studio',entity_id='unrelated')
    except ConflictError:pass
    else:raise RuntimeError('Another operation stole unknown-result window')
    for body in (f.start_body([ident]),{'action':'cancel','job_id':ident},{'action':'close_unknown','job_id':ident}):
        try:await f.manager.command(f.owner,body)
        except ConflictError:pass
        else:raise RuntimeError('Unknown-result action bypassed review fence')
    assert_posting_report(f,0)
    result=await f.manager.command(f.owner,{'action':'close_unknown','job_id':ident,'confirm_no_repost':True})
    require(result['will_repost'] is False,'unknown resolution promises repost')
    await f.manager.tasks[ident]
    require(f.manager.get(f.owner,ident)['status']=='unknown_closed' and not rows(f.db,'browser_operation_leases'),'explicit unknown close failed to retire hold')
    require(f.published==[ident] and not rows(f.db,'posting_receipts'),'explicit unknown close created share/success')
    require(f.manager.snapshot(f.owner)['totals']['failed']==0,'closed unknown result became false failure')
    assert_posting_report(f,0)
    return {'verified':True,'synthetic_publish_calls':1,'receipts':0,'unknown_not_success_or_failure':True,
            'restart_submit_fence':True,'expired_lease_survives_generic_startup':True,'foreign_operation_blocked':True,
            'automatic_replay_rejected':True,'explicit_close_requires_confirmation':True,'explicit_close_never_reposts':True,
            'unknown_report_zero':True}


async def close_case(directory):
    f=Fixture(directory,'negative-close');ident=(await f.prepared())[0];f.close_reply=False
    await f.execute(ident);job=f.manager.get(f.owner,ident);token=job['lease_token']
    require(job['status']=='cleanup_pending' and token and len(rows(f.db,'posting_receipts'))==1,'negative close lost durable success or released hold')
    require(rows(f.db,'browser_operation_leases')[0]['lease_token']==token and f.manager.snapshot(f.owner)['jobs'],'negative close hid or unlocked job')
    require(ident in f.manager.executors,'negative close discarded owned executor')
    assert_posting_report(f,1)
    f.close_reply=True
    await f.manager.command(f.owner,{'action':'retry_cleanup','job_id':ident});await f.manager.tasks[ident]
    require(f.manager.get(f.owner,ident)['status']=='completed' and not rows(f.db,'browser_operation_leases'),'positive retry failed to retire exact hold')
    require(f.published==[ident] and len(rows(f.db,'posting_receipts'))==1,'cleanup retry repeated share or receipt')
    require(f.closed==[('offline-window-0',False),('offline-window-0',True)],'close acknowledgement sequence changed')
    return {'verified':True,'negative_close_retains_lease_card_and_executor':True,'receipt_survives_close_failure':True,
            'cleanup_retry_no_repost':True,'same_lease_through_retry':True,'positive_close_releases_once':True,
            'synthetic_publish_calls':1,'receipts':1}


async def dedup_case(directory):
    from PIL import Image
    f=Fixture(directory,'global-dedup');ident=(await f.prepared())[0];asset=f.manager._asset(f.manager.get(f.owner,ident)['asset_id'])
    original=f.provider.payloads[asset['download_url']]
    other=(await f.generate(owner=f.other,request='cross-owner-request'))[0]
    item={key:asset[key] for key in ('provider_id','source_url','download_url','photographer','photographer_url')}
    require(f.provider.reserve(other,item) is None,'global provider identity repeated across owners')
    f.provider.override=original
    await f.manager._prepare(f.manager.get(f.other,other))
    require(f.manager.get(f.other,other)['status']=='failed','global source-byte duplicate entered ready queue')
    require(f.manager._asset(f.manager.get(f.other,other)['asset_id'])['state']=='duplicate','source duplicate lost registry fence')
    normalized=io.BytesIO()
    with Image.open(io.BytesIO(original)) as image:image.save(normalized,'PNG')
    require(hashlib.sha256(normalized.getvalue()).digest()!=hashlib.sha256(original).digest(),'normalized fixture did not change source bytes')
    third=(await f.generate(owner=f.other,request='normalized-copy-request'))[0]
    f.provider.override=normalized.getvalue()
    await f.manager._prepare(f.manager.get(f.other,third))
    require(f.manager.get(f.other,third)['status']=='failed','normalized-image duplicate entered ready queue')
    require(f.manager._asset(f.manager.get(f.other,third)['asset_id'])['state']=='duplicate','normalized duplicate lost registry fence')
    require(len(rows(f.db,'posting_assets'))==3 and not rows(f.db,'posting_receipts'),'duplicate screening lost registry or fabricated success')
    return {'verified':True,'global_provider_id_fence':True,'global_original_sha256_fence':True,
            'global_normalized_sha256_fence':True,'cross_owner_dedup':True,'duplicate_registry_retained':True,
            'duplicates_cannot_become_ready':True,'synthetic_publish_calls':0,'receipts':0}


async def run_selftest():
    def forbidden(*args,**kwargs):raise RuntimeError('Offline posting proof attempted network access')
    with tempfile.TemporaryDirectory(prefix='Juxin-PostingWorkflow-') as temporary, ExitStack() as guard:
        for name in ('connect','connect_ex','sendto'):guard.enter_context(patch.object(socket.socket,name,forbidden))
        for name in ('create_connection','getaddrinfo','gethostbyname','gethostbyname_ex'):guard.enter_context(patch.object(socket,name,forbidden))
        directory=Path(temporary)
        cases={'admission_and_review':await admission_case(directory),'confirmed_success':await success_case(directory),
               'unknown_restart':await unknown_case(directory),'negative_close':await close_case(directory),
               'global_material_dedup':await dedup_case(directory)}
    return {'verified':True,'synthetic':True,'network_disabled':True,'live_accounts_tested':False,
            'live_pexels_tested':False,'live_instagram_posted':False,'user_data_touched':False,
            'production_manager_service_and_database':True,'production_material_registry_and_cleanup':True,
            'production_positive_close_fence':True,'cases':cases}


def main():
    print(PROOF_PREFIX+json.dumps(asyncio.run(run_selftest()),sort_keys=True))
