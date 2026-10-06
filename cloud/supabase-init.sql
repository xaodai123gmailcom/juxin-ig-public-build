-- 聚鑫国际：在当前 Supabase 项目的 SQL Editor 执行一次。
-- 只新增此应用的备份表与函数；不会删除或覆盖现有表。
begin;
create table if not exists public.juxin_workspaces (
  owner_id uuid primary key references auth.users(id) on delete cascade,
  revision bigint not null default 1 check (revision > 0),
  payload jsonb not null,
  updated_at timestamptz not null default now(),
  constraint juxin_payload_size check (octet_length(payload::text) <= 45000000)
);
alter table public.juxin_workspaces enable row level security;
revoke all on public.juxin_workspaces from anon;
grant select, insert, update on public.juxin_workspaces to authenticated;
drop policy if exists juxin_own_workspace on public.juxin_workspaces;
create policy juxin_own_workspace on public.juxin_workspaces
  for all to authenticated using ((select auth.uid()) = owner_id)
  with check ((select auth.uid()) = owner_id);
create or replace function public.juxin_save_workspace(expected_revision bigint, new_payload jsonb)
returns bigint language plpgsql security invoker set search_path = '' as $$
declare result_revision bigint;
begin
  if auth.uid() is null then raise exception 'authentication required' using errcode='42501'; end if;
  if expected_revision = 0 then
    insert into public.juxin_workspaces(owner_id, revision, payload)
    values(auth.uid(), 1, new_payload) on conflict (owner_id) do nothing
    returning revision into result_revision;
  else
    update public.juxin_workspaces set revision=revision+1, payload=new_payload, updated_at=now()
    where owner_id=auth.uid() and revision=expected_revision
    returning revision into result_revision;
  end if;
  if result_revision is null then raise exception 'workspace revision conflict' using errcode='40001'; end if;
  return result_revision;
end;
$$;
revoke all on function public.juxin_save_workspace(bigint,jsonb) from public, anon;
grant execute on function public.juxin_save_workspace(bigint,jsonb) to authenticated;
notify pgrst, 'reload schema';
commit;
