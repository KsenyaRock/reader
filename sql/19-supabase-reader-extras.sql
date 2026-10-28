-- Читалка, крок 19: скасування озвучки + синхронізація позиції між телефонами.
-- Безпечно запускати повторно. Чіпає лише reader_* (нова таблиця, нові функції, одна функція оновлена).

-- 1. Позиція прослуховування: «профіль + книга» (профіль задається в читалці, наприклад «Анна»)
create table if not exists public.reader_progress (
  profile text not null,
  book_id text not null,
  pos_sec numeric not null default 0,
  total_sec numeric not null default 0,
  updated timestamptz not null default now(),
  primary key (profile, book_id)
);
alter table public.reader_progress enable row level security;
revoke all on public.reader_progress from anon, authenticated;

create or replace function public.reader_progress_set(p_profile text, p_book text, p_pos numeric, p_total numeric) returns void
language sql security definer set search_path = public, extensions as $fn$
  insert into public.reader_progress (profile, book_id, pos_sec, total_sec, updated)
  values (left(p_profile, 40), left(p_book, 40), greatest(0, p_pos), greatest(0, p_total), now())
  on conflict (profile, book_id) do update
    set pos_sec = excluded.pos_sec, total_sec = excluded.total_sec, updated = now();
$fn$;

create or replace function public.reader_progress_get(p_profile text, p_book text) returns jsonb
language sql security definer set search_path = public, extensions as $fn$
  select jsonb_build_object('pos', pos_sec, 'total', total_sec, 'updated', updated)
    from public.reader_progress where profile = left(p_profile, 40) and book_id = left(p_book, 40);
$fn$;

-- 2. Скасування озвучки користувачем (Edge Function): задача стає «failed» з причиною «Скасовано»
create or replace function public.reader_job_cancel(p_id bigint) returns boolean
language plpgsql security definer set search_path = public, extensions as $fn$
declare n int;
begin
  update public.reader_jobs set status = 'failed', error = 'Скасовано', updated = now()
   where id = p_id and status in ('queued', 'running');
  get diagnostics n = row_count;
  return n > 0;
end $fn$;

-- 3. Повідомлення «Не вдалося озвучити» не надсилаємо, якщо озвучку скасували самі
create or replace function public.reader_notify_failed() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
declare r record; msg text;
begin
  if new.status = 'failed' and old.status is distinct from 'failed' and new.error is distinct from 'Скасовано' then
    msg := '❌ Не вдалося озвучити: ' || new.title || coalesce(E'\nПричина: ' || nullif(new.error, ''), '') || E'\n\nМожна спробувати ще раз у читалці: «+» → «Озвучити книгу».';
    for r in select * from public.tg_targets() t where public.tg_pref(t.prefs, 'readerDone', true) loop
      perform public.tg_send(r.chat_id, msg);
    end loop;
  end if;
  return new;
exception when others then
  raise notice 'reader_notify_failed: %', sqlerrm;
  return new;
end $fn$;
revoke all on function public.reader_notify_failed() from public, anon, authenticated;

-- 4. Права: лише для service_role (Edge Function)
revoke all on function public.reader_progress_set(text, text, numeric, numeric) from public, anon, authenticated;
revoke all on function public.reader_progress_get(text, text) from public, anon, authenticated;
revoke all on function public.reader_job_cancel(bigint) from public, anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_progress_set(text, text, numeric, numeric), public.reader_progress_get(text, text),
      public.reader_job_cancel(bigint) to service_role;
  end if;
end $$;
