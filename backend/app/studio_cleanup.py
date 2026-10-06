"""Owner-scoped local media cleanup; publication history is never removed."""
from __future__ import annotations
import json
from pathlib import Path
from .errors import ConflictError, NotFoundError, ValidationError
from .service import isoformat


def asset_ids(job):
    config=json.loads(job['config_json']);result=json.loads(job['result_json'])
    prepared=result.get('prepared',{})
    if 'asset_ids' in prepared:return set(prepared['asset_ids'])
    if 'asset_ids' in result:return set(result['asset_ids'])
    # Switching a template to AI/Pexels can leave old manual IDs in its config;
    # those files were not published or selected by the automatic job.
    return set(config.get('asset_ids',[])) if config.get('source','manual')=='manual' else set()


def cleanup_materials(manager, owner, requested=None, job_id=None, *, active_ids=()):
    media=manager.media;deleted=[];skipped=[];file_count=0
    # The DB writer prevents enqueue/delete races. File removal keeps originals
    # until all derived copies were removed, so a failed cleanup can be retried.
    with manager.db.write() as c:
        jobs=[dict(r) for r in c.execute('SELECT * FROM studio_jobs WHERE owner_user_id=?',(owner,))]
        leased={r[0] for r in c.execute("SELECT entity_id FROM browser_operation_leases WHERE operation_type='studio'")}
        active=set(active_ids)
        windows={r['profile_id']:r['name'] for r in c.execute('SELECT profile_id,name FROM account_window_plans WHERE owner_user_id=?',(owner,))}
        refs={j['id']:asset_ids(j) for j in jobs}
        published=[j for j in jobs if j['kind']=='posting' and j['status']=='completed' and json.loads(j['result_json']).get('published')==1]
        if job_id:
            job=next((j for j in published if j['id']==job_id),None)
            if not job:raise ConflictError('只有已确认发布成功的任务才能清理素材')
            wanted=refs[job_id]
        elif requested is not None:
            wanted={str(requested)}
        else:
            wanted=set().union(*(refs[j['id']] for j in published)) if published else set()
        for ident in wanted:
            row=c.execute('SELECT * FROM studio_assets WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
            if row is None:
                if requested is not None:raise NotFoundError('素材不存在')
                continue
            if not row['path']:
                if requested is not None:deleted.append(ident)
                continue  # Idempotent manual deletion also removes stale UI cards.
            allowed={'completed','cancelled'}
            if requested is not None:allowed.add('failed')
            blockers=[j for j in jobs if ident in refs[j['id']] and (j['status'] not in allowed or j['inflight'] or j['id'] in leased or j['id'] in active)]
            if blockers:
                details=[]
                for job in blockers:
                    if job['inflight'] or job['status']=='needs_review':
                        reason='结果待确认，请在异常任务中核验发布结果'
                    elif job['id'] in leased or job['id'] in active:
                        reason='任务正在执行或释放窗口，请稍后重试'
                    elif job['status']=='failed':
                        reason='旧失败任务保留素材；不再重试时可点素材卡片的删除素材'
                    else:
                        reason='待执行或已暂停任务仍在使用，请先取消或完成任务'
                    details.append({'job_id':job['id'],'window_name':windows.get(job['profile_id']) or job['profile_id'] or '素材备稿','status':job['status'],'reason':reason})
                label='；'.join(f"{d['window_name']} · 任务 {d['job_id'][:8]}：{d['reason']}" for d in details[:5])
                skipped.append({'asset_id':ident,'reason':label,'blockers':details});continue
            asset=dict(row)
            canonical=Path(asset['path'])
            data_root=manager.db.path.parent.resolve()
            original_roots=[(media.root/owner).resolve(),(manager.db.path.parent/'cloud-media'/owner).resolve()]
            desktop_root=media.files.owner_folder(owner).resolve()
            if not any(root.is_relative_to(data_root) and canonical.resolve().is_relative_to(root) for root in original_roots) or not desktop_root.is_relative_to(media.files.root.resolve()):
                if requested is not None:raise ValidationError('素材路径不属于应用管理目录，已停止清理')
                skipped.append({'asset_id':ident,'reason':'素材路径不属于应用管理目录'});continue
            copies={media.files.selection_path(owner,asset)}
            related=[j for j in jobs if ident in refs[j['id']]]
            for j in related:
                result=json.loads(j['result_json'])
                for item in result.get('prepared',{}).get('files',[]):
                    if item.get('asset_id')==ident and item.get('path'):
                        path=Path(item['path'])
                        # Old cloud-restored paths can belong to a different PC.
                        if path.is_absolute() and path.resolve().is_relative_to(desktop_root):copies.add(path)
            parents=set()
            if any(not path.resolve().is_relative_to(desktop_root) for path in copies):
                if requested is not None:raise ValidationError('桌面素材路径不属于当前用户，已停止清理')
                skipped.append({'asset_id':ident,'reason':'桌面素材路径不属于当前用户'});continue
            try:
                for path in sorted(copies,key=str):
                    if path.is_file():path.unlink();file_count+=1
                    # Match the canonical owner boundary even when saved paths
                    # use Windows 8.3 aliases or a desktop directory symlink.
                    parents.add(path.parent.resolve())
                shared=c.execute("SELECT 1 FROM studio_assets WHERE path=? AND id<>?",(str(canonical),ident)).fetchone()
                if not shared and canonical.is_file():canonical.unlink();file_count+=1
            except OSError as exc:
                if requested is not None:raise ValidationError('部分素材被占用或无法删除，请关闭占用文件的程序后重试；发帖历史仍保留') from exc
                skipped.append({'asset_id':ident,'reason':'文件被占用或无法删除，请关闭占用程序后重试'});continue
            c.execute("UPDATE studio_assets SET path='',preview='' WHERE id=? AND owner_user_id=?",(ident,owner))
            for j in related:
                result=json.loads(j['result_json']);cleaned=set(result.get('cleaned_asset_ids',[]));cleaned.add(ident)
                result['cleaned_asset_ids']=sorted(cleaned);result['media_cleaned_at']=isoformat()
                j['result_json']=json.dumps(result,ensure_ascii=False)
                c.execute('UPDATE studio_jobs SET result_json=? WHERE id=?',(j['result_json'],j['id']))
            for folder in parents:
                while folder!=desktop_root and folder.is_relative_to(desktop_root):
                    try:folder.rmdir()
                    except OSError:break
                    folder=folder.parent
            try:desktop_root.rmdir()
            except OSError:pass
            deleted.append(ident)
    return {'deleted_ids':deleted,'deleted_files':file_count,'skipped':skipped}
