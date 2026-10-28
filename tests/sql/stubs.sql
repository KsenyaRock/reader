-- заглушки того, що в Supabase є з бюджету (для незалежного тесту скрипта читалки)
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin; end if;
end $$;
create table if not exists public.sent (id serial primary key, chat text, msg text);
create or replace function public.tg_send(cid text, msg text, kb jsonb default null) returns void language sql as $$ insert into public.sent(chat, msg) values (cid, msg) $$;
create or replace function public.tg_targets() returns table(uid uuid, chat_id text, name text, prefs jsonb) language sql as $$
  select gen_random_uuid(), '111', 'Олег', '{}'::jsonb union all select gen_random_uuid(), '222', 'Анна', '{"readerDone": false}'::jsonb $$;
create or replace function public.tg_pref(prefs jsonb, key text, def boolean) returns boolean language sql as $$
  select coalesce((prefs ->> key)::boolean, def) $$;
