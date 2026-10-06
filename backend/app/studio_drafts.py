"""Explicit, durable one-draft/one-window assignment with atomic enqueue."""
import hashlib,json,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .errors import ConflictError,NotFoundError,ValidationError
from .service import isoformat

def assignments_from(body):
    rows=body.get('assignments')
    if not isinstance(rows,list) or not 1<=len(rows)<=500:raise ValidationError('请选择备稿并指定窗口')
    pairs=[]
    for row in rows:
        if not isinstance(row,dict):raise ValidationError('备稿分配格式无效')
        draft,profile=row.get('draft_id'),row.get('profile_id')
        if not isinstance(draft,str) or not draft or not isinstance(profile,str) or not profile or len(profile)>200:raise ValidationError('每份备稿都需要指定窗口')
        pairs.append((draft,profile))
    if len({x[0] for x in pairs})!=len(pairs):raise ValidationError('同一批不能重复选择备稿')
    if len({x[1] for x in pairs})!=len(pairs):raise ValidationError('同一批每个窗口只能分配一份备稿')
    return pairs

def load_draft(c,owner,ident):
    row=c.execute("SELECT * FROM studio_jobs WHERE owner_user_id=? AND id=? AND kind='material' AND deleted_at IS NULL",(owner,ident)).fetchone()
    if not row:raise NotFoundError('备稿不存在')
    prepared=json.loads(row['result_json']).get('prepared')
    if row['status']!='completed' or not prepared or not prepared.get('asset_ids'):raise ConflictError('备稿尚未准备完成')
    return row,prepared

def check_unassigned(c,owner,ident):
    if c.execute("SELECT 1 FROM studio_jobs WHERE owner_user_id=? AND source_draft_id=?",(owner,ident)).fetchone():
        raise ConflictError('备稿已分配过发布任务，请查看该任务结果；不会重复分配')

def set_targets(manager,owner,body):
    pairs=assignments_from(body)
    with manager.db.write() as c:
        for ident,profile in pairs:
            load_draft(c,owner,ident);check_unassigned(c,owner,ident)
            if c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(profile,)).fetchone():raise ConflictError('目标窗口正被任务占用，请等待释放')
            c.execute('UPDATE studio_jobs SET draft_target_profile_id=?,updated_at=? WHERE id=? AND owner_user_id=?',(profile,isoformat(),ident,owner))
    return {'saved':len(pairs)}

def start_drafts(manager,owner,body):
    from .studio import config_for
    pairs=assignments_from(body);request=body.get('request_id','')
    if not isinstance(request,str) or not 10<=len(request)<=100:raise ValidationError('缺少任务请求标识')
    schedule=config_for('posting',{k:body[k] for k in ('concurrency','interval_seconds','scheduled_at') if k in body})
    try:
        due=datetime.fromisoformat(schedule['scheduled_at'].replace('Z','+00:00')) if schedule['scheduled_at'] else datetime.now(timezone.utc)
        if due.tzinfo is None:raise ValueError()
        due=due.astimezone(timezone.utc)
    except ValueError:raise ValidationError('计划时间必须包含时区') from None
    jobs=[];seen={};hashes={}
    with manager.db.write() as c:
        for index,(ident,profile) in enumerate(pairs):
            key=request+':draft:'+ident
            existing=c.execute('SELECT id,profile_id FROM studio_jobs WHERE owner_user_id=? AND request_key=?',(owner,key)).fetchone()
            if existing:
                if existing['profile_id']!=profile:raise ConflictError('相同请求不能改换窗口')
                jobs.append(existing['id']);continue
            row,prepared=load_draft(c,owner,ident);check_unassigned(c,owner,ident)
            if row['draft_target_profile_id']!=profile:raise ConflictError('备稿的指定窗口已改变，请刷新后核对分配')
            if c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(profile,)).fetchone():raise ConflictError('目标窗口正被任务占用，请等待释放')
            for asset_id in prepared['asset_ids']:
                asset=c.execute('SELECT path FROM studio_assets WHERE owner_user_id=? AND id=?',(owner,asset_id)).fetchone()
                if not asset or not asset['path'] or not Path(asset['path']).is_file():raise ValidationError('备稿素材已清理或文件缺失，请重新准备')
                path=asset['path']
                if path not in hashes:
                    with open(path,'rb') as stream:hashes[path]=hashlib.file_digest(stream,'sha256').hexdigest()
                digest=hashes[path]
                if digest in seen and seen[digest]!=ident:raise ConflictError('所选备稿包含重复素材，请换成不同素材后再分配多窗口')
                seen[digest]=ident
            config=json.loads(row['config_json'])
            config.update(source='manual',asset_ids=prepared['asset_ids'],caption=prepared['caption'],hashtags='',location=prepared.get('location',''),auto_caption=False,
                          concurrency=schedule['concurrency'],interval_seconds=schedule['interval_seconds'],scheduled_at=schedule['scheduled_at'])
            config=config_for('posting',config)
            job=str(uuid.uuid4());now=isoformat()
            # Both the immutable source binding and its prepared content commit
            # together. A replay or another command cannot claim the draft twice.
            c.execute('''INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,result_json,due_at,created_at,updated_at,source_draft_id)
                         VALUES(?,?,?,'posting',?,?,1,?,?,?,?,?)''',
                      (job,owner,key,profile,json.dumps(config,ensure_ascii=False),json.dumps({'prepared':prepared},ensure_ascii=False),isoformat(due+timedelta(seconds=index*schedule['interval_seconds'])),now,now,ident))
            jobs.append(job)
    return {'job_ids':jobs}
