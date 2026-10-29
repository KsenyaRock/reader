-- Читалка: черга озвучки (до 5 книг чекають, одна озвучується). Запускати в Supabase -> SQL Editor -> New query -> Run.
-- Можна запускати повторно. Потрібні скрипти 7, 8, 9, 18, 19 і 20. Чужі таблиці й функції (бюджет тощо) не змінюються.
-- Розгортати ПЕРЕД новою версією Edge Function reader-voice. Стара функція (reader_job_create) лишається і працює як раніше.

-- 1. Позначка «передано в GitHub»: порожня = книга чекає в черзі (статус лишається queued, читалка показує «у черзі»).
--    Існуючі рядки й задачі зі старої функції отримують час створення, тобто вважаються вже запущеними.
alter table public.reader_jobs add column if not exists dispatched timestamptz default now();

-- 2. Чи є зараз озвучка, що йде (запущена в GitHub не більше 6 годин тому)
create or replace function public.reader_queue_busy() returns boolean
language sql security definer set search_path = public, extensions as $fn$
  select exists (select 1 from public.reader_jobs
                  where status in ('queued', 'running') and dispatched is not null and dispatched > now() - interval '6 hours');
$fn$;

-- 3. Нова задача в чергу (service_role). Не більше 5 книг, що чекають; помилка 55007 = черга заповнена
create or replace function public.reader_job_enqueue(p_title text, p_gender text, p_url text, p_for text default 'both') returns bigint
language plpgsql security definer set search_path = public, extensions as $fn$
declare nid bigint; aud text := coalesce(nullif(p_for, ''), 'both');
begin
  if p_url is null or p_url !~ '^https://' or length(p_url) > 500 then
    raise exception 'bad url' using errcode = '22023';
  end if;
  if aud <> 'both' and not exists (select 1 from public.reader_people where key = aud) then
    raise exception 'bad audience' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(7210021);
  if (select count(*) from public.reader_jobs where status = 'queued' and dispatched is null) >= 5 then
    raise exception 'queue full' using errcode = '55007';
  end if;
  insert into public.reader_jobs (title, gender, epub_url, audience, dispatched)
  values (left(coalesce(nullif(p_title, ''), 'без назви'), 300),
          case when p_gender in ('male', 'apple') then p_gender else 'female' end, p_url, aud, null)
  returning id into nid;
  return nid;
end $fn$;

-- 4. Взяти наступну книгу з черги (service_role). Повертає {id, gender, epub_url} або null.
--    Нічого не бере, якщо вже йде озвучка. Одразу після скасування чекає 20 с: Edge Function встигає зупинити старий
--    запуск у GitHub і не зачіпає новий.
create or replace function public.reader_queue_claim() returns jsonb
language plpgsql security definer set search_path = public, extensions as $fn$
declare j record;
begin
  perform pg_advisory_xact_lock(7210021);
  if public.reader_queue_busy() then return null; end if;
  if exists (select 1 from public.reader_jobs where error = 'Скасовано' and updated > now() - interval '20 seconds') then return null; end if;
  select id, gender, epub_url into j from public.reader_jobs
   where status = 'queued' and dispatched is null order by id limit 1 for update skip locked;
  if not found then return null; end if;
  update public.reader_jobs set dispatched = now(), updated = now() where id = j.id;
  return jsonb_build_object('id', j.id, 'gender', j.gender, 'epub_url', j.epub_url);
end $fn$;

-- 5. Скасування (service_role): повертає 'waiting' (книга ще чекала в черзі, GitHub не чіпати), 'active' (вже йшла) або null
create or replace function public.reader_job_cancel2(p_id bigint) returns text
language plpgsql security definer set search_path = public, extensions as $fn$
declare prev record;
begin
  select status, dispatched into prev from public.reader_jobs where id = p_id and status in ('queued', 'running') for update;
  if not found then return null; end if;
  update public.reader_jobs set status = 'failed', error = 'Скасовано', updated = now() where id = p_id;
  return case when prev.dispatched is null then 'waiting' else 'active' end;
end $fn$;

-- 6. Готова книга успадковує «для кого» від озвучки, що ЙДЕ (а не від книги, що ще чекає в черзі)
create or replace function public.reader_book_audience() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
declare a text;
begin
  if new.audience is null or new.audience = 'both' then
    select audience into a from public.reader_jobs
     where status in ('queued', 'running') and dispatched is not null and dispatched > now() - interval '6 hours'
     order by dispatched desc, id desc limit 1;
    new.audience := coalesce(a, 'both');
  end if;
  return new;
end $fn$;

-- 7. «Штовхач» черги: якщо книги чекають і нічого не йде, просить Edge Function (дія tick) запустити наступну.
--    Викликається тригером у момент завершення озвучки й cron раз на 2 хвилини (запасний шлях). Помилка мережі нічого не ламає.
create or replace function public.reader_queue_kick() returns void
language plpgsql security definer set search_path = public, extensions as $fn$
declare c text;
begin
  if not exists (select 1 from public.reader_jobs where status = 'queued' and dispatched is null) then return; end if;
  if public.reader_queue_busy() then return; end if;
  select voice_code into c from public.reader_settings where id = 1;
  if c is null then return; end if;
  begin
    execute 'select net.http_post(url := $1, body := $2, headers := $3, timeout_milliseconds := 20000)'
      using 'https://hikzqzdulvekuplolymw.supabase.co/functions/v1/reader-voice',
            jsonb_build_object('action', 'tick', 'code', c), '{"Content-Type":"application/json"}'::jsonb;
  exception when others then
    raise notice 'reader_queue_kick: %', sqlerrm;
  end;
end $fn$;

create or replace function public.reader_queue_after_job() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
begin
  -- після скасування наступну книгу бере cron (пауза 20 с у reader_queue_claim), решта випадків одразу
  if new.error is distinct from 'Скасовано' then perform public.reader_queue_kick(); end if;
  return new;
exception when others then
  raise notice 'reader_queue_after_job: %', sqlerrm;
  return new;
end $fn$;
drop trigger if exists reader_jobs_queue on public.reader_jobs;
create trigger reader_jobs_queue after update of status on public.reader_jobs
  for each row when (new.status in ('done', 'failed') and old.status in ('queued', 'running'))
  execute function public.reader_queue_after_job();

do $$ begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule('reader_queue_kick', '*/2 * * * *', 'select public.reader_queue_kick()');
  end if;
end $$;

-- 8. Права: службові функції лише для service_role (Edge Function)
revoke all on function public.reader_queue_busy() from public, anon, authenticated;
revoke all on function public.reader_job_enqueue(text, text, text, text) from public, anon, authenticated;
revoke all on function public.reader_queue_claim() from public, anon, authenticated;
revoke all on function public.reader_job_cancel2(bigint) from public, anon, authenticated;
revoke all on function public.reader_queue_kick() from public, anon, authenticated;
revoke all on function public.reader_queue_after_job() from public, anon, authenticated;
revoke all on function public.reader_book_audience() from public, anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_job_enqueue(text, text, text, text), public.reader_queue_claim(),
      public.reader_job_cancel2(bigint) to service_role;
  end if;
end $$;
