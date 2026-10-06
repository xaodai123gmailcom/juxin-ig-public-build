"""Atomic creation of independent, unopened native windows; retry-idempotent."""
import hashlib
import json
import uuid
from pydantic import ValidationError as ModelError
from .errors import ConflictError, ValidationError
from .service import isoformat


def initialize_batch_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS account_creation_batches(
        owner_user_id TEXT NOT NULL REFERENCES app_users(id),request_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(owner_user_id,request_key))''')
    if not c.execute("SELECT 1 FROM schema_migrations WHERE version=29").fetchone():
        from .identity_registry import repair_global_registry
        repair_global_registry(c)
        c.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(29,datetime('now'))")


def create_batch(workspace, owner, body):
    from .account_workspace import WindowPlan
    from .native_browser import validate_proxy
    try:
        request_key = str(uuid.UUID(str(body.get('request_key', ''))))
    except (ValueError, TypeError):
        raise ValidationError('批量创建请求编号无效') from None
    count, start = body.get('count'), body.get('start', 1)
    if type(count) is not int or count < 1 or type(start) is not int or start < 1:
        raise ValidationError('创建数量和起始序号必须是正整数')
    prefix = str(body.get('prefix', '')).strip()
    if not prefix or len(f'{prefix} {start+count-1}') > 80:
        raise ValidationError('请填写窗口名称前缀，名称加序号不能超过 80 个字符')
    payload = {k: body[k] for k in ('platform', 'group', 'proxy_server', 'language', 'timezone') if k in body}
    try:
        config = WindowPlan.model_validate({**payload, 'name': prefix, 'native': True})
    except ModelError as e:
        raise ValidationError('; '.join(v['msg'] for v in e.errors())) from None
    config.proxy_server = validate_proxy(config.proxy_server)
    native = getattr(workspace.bitbrowser, 'native', None)
    if native is None:
        raise ValidationError('当前运行版本不支持内置窗口')
    fingerprint = hashlib.sha256(json.dumps({'config': config.model_dump(), 'count': count, 'start': start}, sort_keys=True).encode()).hexdigest()
    now = isoformat()
    env = json.dumps({'platform': config.platform, 'language': config.language, 'timezone': config.timezone, 'proxy_note': ''}, ensure_ascii=False)
    with workspace.db.write() as c:
        existing = c.execute('SELECT fingerprint,result_json FROM account_creation_batches WHERE owner_user_id=? AND request_key=?', (owner, request_key)).fetchone()
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise ConflictError('这批窗口的创建参数已变化，请重新发起创建')
            return {**json.loads(existing['result_json']), 'reused': True}
        serial = c.execute('SELECT COALESCE(MAX(serial),0)+1 FROM account_window_plans WHERE owner_user_id=?', (owner,)).fetchone()[0]
        ids = []
        for i in range(count):
            name, ident = f'{prefix} {start+i}', str(uuid.uuid4())
            profile = native.create(c, owner, name, config.group.strip(), config.proxy_server)
            c.execute('''INSERT INTO account_window_plans(id,owner_user_id,serial,name,group_name,profile_id,environment_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (ident, owner, serial+i, name, config.group.strip(), profile, env, now, now))
            workspace._event(c, owner, ident, name, '批量新建方案')
            ids.append(ident)
        result = {'id': ids[0], 'ids': ids, 'created': count, 'message': f'已创建 {count} 个独立窗口，等待登录'}
        c.execute('INSERT INTO account_creation_batches VALUES(?,?,?,?,?)', (owner, request_key, fingerprint, json.dumps(result, ensure_ascii=False), now))
    return result
