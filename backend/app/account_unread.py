"""Passive unread observations; never navigate, open windows, or take task leases."""
from datetime import datetime, timezone


def account_unread_snapshot(db,native,owner):
    with db.read() as c:
        profiles={r[0] for r in c.execute("SELECT profile_id FROM account_window_plans WHERE owner_user_id=? AND archived=0 AND profile_id<>''",(owner,))}
    try:
        read=getattr(native,'unread_snapshot',None)
        raw=read(owner).get('windows',{}) if callable(read) else {}
    except Exception:raw={}
    result={};total=0;capped=False;unknown=0
    for ident in profiles:
        row=raw.get(ident,{})
        count=row.get('count')
        if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=1_000_000:count=None
        status=row.get('status','unavailable')
        if status not in {'live','stale','closed','unavailable','signed_out','storage_error'}:status='unavailable'
        stamp=row.get('observed_at')
        try:datetime.fromisoformat(stamp.replace('Z','+00:00'))
        except (ValueError,AttributeError,TypeError):stamp=None;count=None
        if status in {'signed_out','unavailable'}:count=None
        result[ident]={'count':count,'capped':row.get('capped') is True,'status':status,'observed_at':stamp}
        if count is None:unknown+=1
        else:total+=count;capped=capped or row.get('capped') is True
    return {'windows':result,'total':total,'capped':capped,'unknown':unknown}
