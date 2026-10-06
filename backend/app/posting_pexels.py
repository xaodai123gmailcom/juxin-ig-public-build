"""Official API only; bounded IO, persistent quota/cache, global ID and streaming hash fences."""
from __future__ import annotations
import base64,hashlib,io,json,os,sqlite3,time,uuid,shutil
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode,urlparse
from urllib.request import Request,build_opener,HTTPRedirectHandler,ProxyHandler
from urllib.error import HTTPError,URLError
from .errors import ValidationError,ConflictError
from .service import isoformat
MAX_BYTES=20*1024*1024
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValidationError('素材服务跳转被拒绝，请稍后重试')
class PexelsProvider:
    name='pexels';label='Pexels'
    def __init__(self,db,key_supplier=None,opener=None,clock=time.time):
        self.db=db;self.key_supplier=key_supplier or (lambda:os.environ.get('IGAC_PEXELS_API_KEY',''))
        self.opener=opener or build_opener(ProxyHandler({}),NoRedirect());self.clock=clock
        self.root=(db.path.parent/'posting-media').resolve()
    def configured(self):return bool(self.key_supplier())
    def validate(self):
        """Explicit button only. Fixed official URL; never returns credential or raw error."""
        if not self.configured():return {'valid':False,'status':'unconfigured','message':'素材服务未配置'}
        now=self.clock();dt=datetime.fromtimestamp(now,timezone.utc)
        with self.db.write() as c:
            wait=c.execute('SELECT until_epoch FROM posting_api_backoff WHERE id=1').fetchone()
            if wait and wait[0]>now:return {'valid':False,'status':'rate_limited','message':'Pexels 限流中，请等待配额恢复'}
            for bucket,limit in [('validate:'+dt.strftime('%Y-%m-%dT%H:%M'),1),('hour:'+dt.strftime('%Y-%m-%dT%H'),200),('month:'+dt.strftime('%Y-%m'),20000)]:
                c.execute('INSERT OR IGNORE INTO posting_api_usage(bucket,count) VALUES(?,0)',(bucket,))
                if c.execute('SELECT count FROM posting_api_usage WHERE bucket=?',(bucket,)).fetchone()[0]>=limit:return {'valid':False,'status':'rate_limited','message':'请稍后再次验证，避免重复消耗配额'}
                c.execute('UPDATE posting_api_usage SET count=count+1 WHERE bucket=?',(bucket,))
        req=Request('https://api.pexels.com/v1/curated?per_page=1',headers={'Authorization':self.key_supplier(),'User-Agent':'Juxin-Posting/1.0'})
        try:
            with self.opener.open(req,timeout=15) as response:
                response.read(65536)
            return {'valid':True,'status':'valid','message':'Pexels 已接受当前凭据；未下载素材'}
        except HTTPError as exc:
            if exc.code==429:self._backoff(exc.headers,now)
            statuses={401:('invalid','Pexels 拒绝凭据，请核对或更换'),403:('forbidden','该凭据当前没有访问权限'),429:('rate_limited','Pexels 配额不足，无法据此判断凭据有效性')}
            status,message=statuses.get(exc.code,('unconfirmed','Pexels 服务异常，尚未确认连接'))
            return {'valid':False,'status':status,'message':message}
        except Exception:return {'valid':False,'status':'unconfirmed','message':'网络或连接异常，尚未确认凭据有效性'}
    def search(self,query,page=1):
        if not self.configured():raise ValidationError('请先由本人在安全配置中输入 Pexels 密钥')
        query=' '.join(str(query).split())[:300]
        if not query or type(page)!=int or not 1<=page<=5:raise ValidationError('素材搜索参数无效')
        cache_key=hashlib.sha256((query.casefold()+':'+str(page)).encode()).hexdigest();now=self.clock()
        with self.db.write() as c:
            cache=c.execute('SELECT response_json FROM posting_api_cache WHERE cache_key=? AND expires_at>?',(cache_key,now)).fetchone()
            if cache:return json.loads(cache[0])
            wait=c.execute('SELECT until_epoch FROM posting_api_backoff WHERE id=1').fetchone()
            if wait and wait[0]>now:raise ValidationError('Pexels 暂时限流，请在配额恢复后重试')
            dt=datetime.fromtimestamp(now,timezone.utc)
            for bucket,limit in [('hour:'+dt.strftime('%Y-%m-%dT%H'),200),('month:'+dt.strftime('%Y-%m'),20000)]:
                c.execute('INSERT OR IGNORE INTO posting_api_usage(bucket,count) VALUES(?,0)',(bucket,))
                if c.execute('SELECT count FROM posting_api_usage WHERE bucket=?',(bucket,)).fetchone()[0]>=limit:raise ValidationError('Pexels 本地配额已用完，请稍后重试')
                c.execute('UPDATE posting_api_usage SET count=count+1 WHERE bucket=?',(bucket,))
        req=Request('https://api.pexels.com/v1/search?'+urlencode({'query':query,'page':page,'per_page':40}),headers={'Authorization':self.key_supplier(),'User-Agent':'Juxin-Posting/1.0'})
        try:
            with self.opener.open(req,timeout=20) as response:
                raw=response.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:raise ValidationError('Pexels 响应过大')
                data=json.loads(raw)
                remaining=response.headers.get('X-Ratelimit-Remaining')
                if remaining=='0':self._backoff(response.headers,now)
        except HTTPError as e:
            if e.code==429:self._backoff(e.headers,now)
            raise ValidationError('Pexels 请求失败：密钥无效或配额不足' if e.code in (401,403,429) else 'Pexels 服务暂不可用') from None
        except (OSError,ValueError,URLError):raise ValidationError('Pexels 网络或响应异常，请稍后重试') from None
        items=[]
        for photo in data.get('photos',[]):
            ident=str(photo.get('id',''));src=photo.get('src',{}).get('large2x','');url=photo.get('url','')
            if not ident.isdigit() or not self._allowed(src,'images.pexels.com') or not self._allowed(url,'www.pexels.com'):continue
            items.append({'provider_id':ident,'download_url':src,'source_url':url,'photographer':str(photo.get('photographer',''))[:200], 'photographer_url':str(photo.get('photographer_url',''))[:500]})
        with self.db.write() as c:c.execute('INSERT OR REPLACE INTO posting_api_cache VALUES(?,?,?)',(cache_key,json.dumps(items),now+86400))
        return items
    def _backoff(self,headers,now):
        try:until=max(now+60,float(headers.get('X-Ratelimit-Reset',now+3600)))
        except (ValueError,TypeError):until=now+3600
        try:until=max(until,now+float(headers.get('Retry-After',0)))
        except (ValueError,TypeError):pass
        with self.db.write() as c:c.execute('INSERT INTO posting_api_backoff VALUES(1,?) ON CONFLICT(id) DO UPDATE SET until_epoch=max(until_epoch,excluded.until_epoch)',(until,))
    @staticmethod
    def _allowed(url,host):
        try:
            p=urlparse(url);return p.scheme=='https' and p.hostname==host and not p.username and not p.password and not p.fragment and p.port in (None,443)
        except (ValueError,TypeError):return False
    def reserve(self,job_id,item):
        asset_id=str(uuid.uuid4())
        try:
            with self.db.write() as c:
                row=c.execute('SELECT status,asset_id FROM posting_jobs WHERE id=?',(job_id,)).fetchone()
                if not row or row['status']!='preparing':raise ConflictError('任务已取消或状态已变化')
                if row['asset_id']:return row['asset_id']
                c.execute('INSERT INTO posting_assets(id,provider,provider_id,job_id,source_url,photographer,photographer_url,download_url,created_at,source_license,license_url,credit,verified_at,provider_sha1) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(asset_id,self.name,item['provider_id'],job_id,item['source_url'],item['photographer'],item.get('photographer_url',''),item['download_url'],isoformat(),item.get('source_license','Pexels License'),item.get('license_url','https://www.pexels.com/license/'),item.get('credit',''),item.get('verified_at',isoformat()),item.get('provider_sha1','')))
                c.execute('UPDATE posting_jobs SET asset_id=? WHERE id=?',(asset_id,job_id))
            return asset_id
        except sqlite3.IntegrityError:return None
    def prepare(self,job):
        with self.db.read() as c:existing=c.execute('SELECT * FROM posting_assets WHERE job_id=?',(job['id'],)).fetchone()
        if existing:
            self.download(dict(existing));return existing['id']
        for page in range(1,6):
            for item in self.search(job['theme'],page):
                item=self.verify_item(item)
                if not item:continue
                ident=self.reserve(job['id'],item)
                if not ident:continue
                with self.db.read() as c:asset=dict(c.execute('SELECT * FROM posting_assets WHERE id=?',(ident,)).fetchone())
                self.download(asset);return ident
        raise ValidationError('没有足够未使用素材，已停止；不会重复使用旧素材')
    def verify_item(self,item):return item
    def download_allowed(self,url):return self._allowed(url,'images.pexels.com')
    def download(self,asset):
        if asset['state']=='ready' and asset['path'] and Path(asset['path']).is_file():return
        if not self.download_allowed(asset['download_url']):raise ValidationError('素材地址无效')
        self.root.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(self.root).free<256*1024*1024:raise ValidationError('磁盘可用空间不足，已暂停下载；不会自动删除可恢复素材')
        part=self.root/(asset['id']+'.part');final=self.root/(asset['id']+'.jpg')
        digest=hashlib.sha256();sha1=hashlib.sha1();size=0
        try:
            with self.opener.open(Request(asset['download_url'],headers={'User-Agent':'Juxin-Posting/1.0'}),timeout=25) as response,part.open('wb') as file:
                for block in iter(lambda:response.read(65536),b''):
                    size+=len(block)
                    if size>MAX_BYTES:raise ValidationError('素材超过 20 MB 限制')
                    digest.update(block);sha1.update(block);file.write(block)
                file.flush();os.fsync(file.fileno())
            if asset.get('provider_sha1') and sha1.hexdigest()!=asset['provider_sha1']:raise ValidationError('下载文件与来源版本不一致')
            from PIL import Image,ImageOps
            with Image.open(part) as original:
                if original.width*original.height>30_000_000 or original.width<320 or original.height<320:raise ValidationError('素材分辨率不符合要求')
                image=ImageOps.exif_transpose(original).convert('RGB');render=io.BytesIO();image.save(render,'JPEG',quality=94);data=render.getvalue()
                if len(data)>MAX_BYTES:raise ValidationError('转换后素材过大')
                image.thumbnail((160,160));thumb=io.BytesIO();image.save(thumb,'JPEG',quality=70)
            rendered=hashlib.sha256(data).hexdigest()
            with self.db.write() as c:
                c.execute("UPDATE posting_assets SET sha256=?,render_sha256=? WHERE id=? AND state='reserved'",(digest.hexdigest(),rendered,asset['id']))
            part.write_bytes(data);os.replace(part,final)
            with self.db.write() as c:
                c.execute("UPDATE posting_assets SET path=?,state='ready',preview=? WHERE id=?",(str(final),'data:image/jpeg;base64,'+base64.b64encode(thumb.getvalue()).decode(),asset['id']))
        except sqlite3.IntegrityError:
            self._quarantine(part,asset['id']+'-duplicate.part')
            with self.db.write() as c:c.execute("UPDATE posting_assets SET state='duplicate' WHERE id=?",(asset['id'],))
            raise ValidationError('素材文件重复，已阻止使用；请创建新任务匹配其他素材') from None
        except Exception:
            self._quarantine(part,asset['id']+'-incomplete.part')
            raise ValidationError('素材下载或校验失败，未发布；请检查网络后重试') from None
    def _quarantine(self,path,name):
        if path.exists():
            recovery=self.root/'recovery';recovery.mkdir(exist_ok=True);os.replace(path,recovery/name)
    def cleanup(self,asset):
        """Only app-owned single-job asset; rename, never unlink. Registry survives."""
        with self.db.read() as c:
            refs=c.execute("SELECT COUNT(*) FROM posting_jobs WHERE asset_id=? AND status NOT IN ('cleanup_pending','completed','cancelled')",(asset['id'],)).fetchone()[0]
        if refs:raise ConflictError('素材仍被未结束任务引用')
        path=Path(asset['path']) if asset['path'] else None;recovery=self.root/'recovery'/(asset['id']+'.jpg')
        if path:
            if path.resolve().parent!=self.root or path.is_symlink():raise ValidationError('拒绝清理非本应用素材')
            recovery.parent.mkdir(parents=True,exist_ok=True)
            if path.exists():os.replace(path,recovery)
            elif not recovery.exists():raise ValidationError('素材文件缺失，不能确认清理完成')
        with self.db.write() as c:c.execute("UPDATE posting_assets SET path='',recovery_path=?,state=CASE WHEN state='used' THEN 'used' ELSE 'retired' END WHERE id=?",(str(recovery),asset['id']))
