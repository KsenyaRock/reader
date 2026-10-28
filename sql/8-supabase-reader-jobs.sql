-- Читалка: озвучка з телефона («Озвучити книгу»). Запускати в Supabase -> SQL Editor -> New query -> Run.
-- Можна запускати повторно. Потрібен скрипт 7 (reader_settings, reader_books). Чужі таблиці й функції не змінюються.

-- 1. Код запуску озвучки (його вводять у читалці). Показати: select voice_code from public.reader_settings;
alter table public.reader_settings add column if not exists voice_code text not null default substr(replace(gen_random_uuid()::text, '-', ''), 1, 12);
alter table public.reader_settings add column if not exists fail_count int not null default 0;
alter table public.reader_settings add column if not exists fail_since timestamptz;

-- 2. Задачі озвучки
create table if not exists public.reader_jobs (
  id bigserial primary key,
  title text not null,
  gender text not null default 'female',
  epub_url text not null,
  status text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed')),
  error text,
  book_id bigint,
  created timestamptz not null default now(),
  updated timestamptz not null default now()
);
alter table public.reader_jobs enable row level security;
revoke all on public.reader_jobs from anon, authenticated;
revoke all on sequence public.reader_jobs_id_seq from anon, authenticated;

-- 3. Перевірка коду (не більше 5 помилок за 15 хвилин). Лише для Edge Function (service_role).
create or replace function public.reader_voice_auth(p_code text) returns boolean
language plpgsql security definer set search_path = public, extensions as $fn$
declare s record;
begin
  select * into s from public.reader_settings where id = 1 for update;
  if s.fail_count >= 5 and s.fail_since > now() - interval '15 minutes' then
    raise exception 'too many attempts' using errcode = '54000';
  end if;
  if p_code is not null and p_code <> '' and p_code = s.voice_code then
    update public.reader_settings set fail_count = 0, fail_since = null where id = 1;
    return true;
  end if;
  update public.reader_settings
     set fail_count = case when s.fail_since is null or s.fail_since <= now() - interval '15 minutes' then 1 else s.fail_count + 1 end,
         fail_since = case when s.fail_since is null or s.fail_since <= now() - interval '15 minutes' then now() else s.fail_since end
   where id = 1;
  return false;
end $fn$;

-- 4. Нова задача: одна активна за раз (активною вважається до 6 годин)
create or replace function public.reader_job_create(p_title text, p_gender text, p_url text) returns bigint
language plpgsql security definer set search_path = public, extensions as $fn$
declare nid bigint;
begin
  if p_url is null or p_url !~ '^https://' or length(p_url) > 500 then
    raise exception 'bad url' using errcode = '22023';
  end if;
  if exists (select 1 from public.reader_jobs where status in ('queued', 'running') and created > now() - interval '6 hours') then
    raise exception 'busy' using errcode = '55006';
  end if;
  insert into public.reader_jobs (title, gender, epub_url)
  values (left(coalesce(nullif(p_title, ''), 'без назви'), 300), case when p_gender = 'male' then 'male' else 'female' end, p_url)
  returning id into nid;
  return nid;
end $fn$;

-- 5. Зміна статусу з Edge Function (service_role)
create or replace function public.reader_job_fail(p_id bigint, p_err text) returns void
language sql security definer set search_path = public, extensions as $fn$
  update public.reader_jobs set status = 'failed', error = left(p_err, 500), updated = now() where id = p_id and status in ('queued', 'running');
$fn$;

-- 6. Список для читалки (service_role): задачі за останні 2 доби й готові книги
create or replace function public.reader_voice_list() returns jsonb
language sql security definer set search_path = public, extensions as $fn$
  select jsonb_build_object(
    'jobs', coalesce((select jsonb_agg(j order by j.id desc) from (
        select id, title, status, error, created from public.reader_jobs
         where created > now() - interval '2 days' and (status <> 'done') order by id desc limit 10) j), '[]'::jsonb),
    'books', coalesce((select jsonb_agg(b order by b.id desc) from (
        select id, title, author, link, duration_sec, size_mb, created from public.reader_books
         where status = 'ready' order by id desc limit 30) b), '[]'::jsonb));
$fn$;

-- 7. Виклик із хмарної задачі (anon-ключ + «ключ програми», як reader_book_ready): running / done / failed
create or replace function public.reader_job_update(k text, p_job bigint, p_status text, p_err text default null) returns jsonb
language plpgsql security definer set search_path = public, extensions as $fn$
declare saved text;
begin
  select upload_key into saved from public.reader_settings where id = 1;
  if saved is null or k is null or k <> saved then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  if p_status not in ('running', 'done', 'failed') then
    raise exception 'bad status' using errcode = '22023';
  end if;
  update public.reader_jobs set status = p_status, error = case when p_status = 'failed' then left(p_err, 500) end, updated = now()
   where id = p_job and status in ('queued', 'running');
  return jsonb_build_object('ok', true);
end $fn$;

-- 8. «Не вдалося озвучити» в Telegram
create or replace function public.reader_notify_failed() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
declare r record; msg text;
begin
  if new.status = 'failed' and old.status is distinct from 'failed' then
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
drop trigger if exists reader_jobs_failed on public.reader_jobs;
create trigger reader_jobs_failed after update on public.reader_jobs
  for each row execute function public.reader_notify_failed();

-- 9. Права: службові функції лише для service_role (Edge Function), виклик із хмарної задачі для anon
revoke all on function public.reader_voice_auth(text) from public, anon, authenticated;
revoke all on function public.reader_job_create(text, text, text) from public, anon, authenticated;
revoke all on function public.reader_job_fail(bigint, text) from public, anon, authenticated;
revoke all on function public.reader_voice_list() from public, anon, authenticated;
revoke all on function public.reader_job_update(text, bigint, text, text) from public;
grant execute on function public.reader_job_update(text, bigint, text, text) to anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_voice_auth(text), public.reader_job_create(text, text, text),
      public.reader_job_fail(bigint, text), public.reader_voice_list() to service_role;
  end if;
end $$;
