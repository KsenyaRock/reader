-- Читалка: захист і точність черги озвучки. Запускати в Supabase -> SQL Editor -> New query -> Run.
-- Можна запускати повторно. Потрібні скрипти 8, 20, 21, 22. Сайт (Netlify) не змінюється. Таблиці бюджету не чіпаються.
-- 1) Блокування невдалих спроб коду окремо для кожної IP-адреси (раніше 5 помилок будь-кого блокували код для всіх, зокрема й чергу).
-- 2) Сторож: список запущених задач, для яких GitHub уже не має активного запуску (їх Edge позначає помилкою й бере наступну книгу).
-- 3) «Штовхач» черги будить Edge також коли є, що перевіряти сторожу; cron раз на 5 хвилин замість 2.
-- 4) Список озвучок: давні помилки й «Скасовано» не показуються (помилки лише за останні 3 години); час черги рахується
--    від моменту запуску, а не від постановки в чергу.
-- Відкат: функція reader_voice_auth зі скрипта 8 і reader_voice_list зі скрипта 22 (запустити їх файли).

-- 1. Спроби коду за IP
create table if not exists public.reader_auth_fails (
  ip text primary key,
  n int not null default 0,
  since timestamptz not null default now()
);
alter table public.reader_auth_fails enable row level security;
revoke all on public.reader_auth_fails from anon, authenticated;

drop function if exists public.reader_voice_auth(text);
create or replace function public.reader_voice_auth(p_code text, p_ip text default null) returns boolean
language plpgsql security definer set search_path = public, extensions as $fn$
declare
  v_ip text := left(coalesce(nullif(p_ip, ''), 'unknown'), 64);   -- 'unknown': виклики самої бази (черга)
  f record; saved text; tot int;
begin
  delete from public.reader_auth_fails where since < now() - interval '1 day';
  select * into f from public.reader_auth_fails where ip = v_ip;
  if found and f.n >= 5 and f.since > now() - interval '15 minutes' then
    raise exception 'too many attempts' using errcode = '54000';
  end if;
  -- запасна межа від розподіленого підбору: понад 60 помилок за 15 хвилин блокують усіх, крім викликів самої бази
  if v_ip <> 'unknown' then
    select coalesce(sum(n), 0) into tot from public.reader_auth_fails where ip <> 'unknown' and since > now() - interval '15 minutes';
    if tot >= 60 then raise exception 'too many attempts' using errcode = '54000'; end if;
  end if;
  select voice_code into saved from public.reader_settings where id = 1;
  if p_code is not null and p_code <> '' and p_code = saved then
    delete from public.reader_auth_fails where ip = v_ip;
    return true;
  end if;
  insert into public.reader_auth_fails as a (ip, n, since) values (v_ip, 1, now())
  on conflict (ip) do update
    set n = case when a.since <= now() - interval '15 minutes' then 1 else a.n + 1 end,
        since = case when a.since <= now() - interval '15 minutes' then now() else a.since end;
  return false;
end $fn$;

-- 2. Запущені задачі, яким може бути потрібна перевірка сторожем (запущені понад p_min хвилин тому)
create or replace function public.reader_stale_jobs(p_min int default 10) returns jsonb
language sql security definer set search_path = public, extensions as $fn$
  select coalesce(jsonb_agg(jsonb_build_object('id', id, 'gender', gender) order by id), '[]'::jsonb) from (
    select id, gender from public.reader_jobs
     where status in ('queued', 'running') and dispatched is not null
       and dispatched < now() - make_interval(mins => greatest(p_min, 1))
     order by id limit 20) s;
$fn$;

-- 3. «Штовхач»: будить Edge, якщо книга чекає й нічого не йде, або є запущена задача, яку варто звірити з GitHub
create or replace function public.reader_queue_kick() returns void
language plpgsql security definer set search_path = public, extensions as $fn$
declare c text; need boolean;
begin
  need := exists (select 1 from public.reader_jobs where status in ('queued', 'running') and dispatched is not null
                   and dispatched < now() - interval '10 minutes')
       or (exists (select 1 from public.reader_jobs where status = 'queued' and dispatched is null) and not public.reader_queue_busy());
  if not need then return; end if;
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

do $$ begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule('reader_queue_kick', '*/5 * * * *', 'select public.reader_queue_kick()');
  end if;
end $$;

-- 4. Список озвучок: без старих помилок і «Скасовано», точніший час черги
create or replace function public.reader_voice_list(p_me text default null) returns jsonb
language plpgsql security definer set search_path = public, extensions as $fn$
declare
  me text := coalesce(p_me, 'both');
  jobs jsonb; sumrow jsonb := null;
  r record; est numeric; total numeric := 0; n int := 0;
  h_key text; h_voice text; h_running boolean := false; h_seen boolean := false;
  ttl text;
  tz text := coalesce((select name from pg_timezone_names where name in ('Europe/Kyiv', 'Europe/Kiev') limit 1), 'UTC');
begin
  select coalesce(jsonb_agg(j order by j.id desc), '[]'::jsonb) into jobs from (
    select rj.id, rj.title, rj.status, rj.error, rj.created, rj.audience from public.reader_jobs rj
     where rj.created > now() - interval '2 days' and rj.audience in ('both', me)
       and (rj.status in ('queued', 'running')
            or (rj.status = 'failed' and rj.updated > now() - interval '3 hours' and rj.error is distinct from 'Скасовано'))
     order by rj.id desc limit 10) j;

  for r in select id, gender, status, audience, created, dispatched from public.reader_jobs
            where status in ('queued', 'running') and created > now() - interval '2 days'
              and (dispatched is null or dispatched > now() - interval '6 hours')
            order by (dispatched is null), id loop
    n := n + 1;
    -- тривалість готових озвучок цим голосом рахується від запуску (без очікування в черзі)
    select percentile_cont(0.5) within group (order by extract(epoch from d.updated - d.dispatched) / 60) into est
      from (select dispatched, updated from public.reader_jobs
             where status = 'done' and gender = r.gender and dispatched is not null order by id desc limit 10) d;
    est := case when est is null then (case when r.gender = 'apple' then 90 else 45 end) else greatest(10, est) end;
    total := total + case when r.dispatched is null then est
                          else greatest(5, est - extract(epoch from now() - r.dispatched) / 60) end;
    if r.audience not in ('both', me) and (not h_seen or (r.dispatched is not null and not h_running)) then
      h_seen := true; h_running := r.dispatched is not null; h_key := r.audience;
      h_voice := case when r.gender = 'apple' then 'Apple' else 'Edge' end;
    end if;
  end loop;

  if h_seen then
    ttl := 'Черга до ≈' || to_char((now() + make_interval(mins => ceil(total)::int)) at time zone tz, 'HH24:MI')
          || ' · книг: ' || n || ' · ' || h_voice;
    sumrow := jsonb_build_object('id', 0, 'title', ttl, 'status', case when h_running then 'running' else 'queued' end,
                                 'error', null, 'created', now(), 'audience', h_key);
    jobs := jsonb_build_array(sumrow) || jobs;
  end if;

  return jsonb_build_object(
    'people', coalesce((select jsonb_agg(jsonb_build_object('key', key, 'label', label) order by key) from public.reader_people), '[]'::jsonb),
    'jobs', jobs,
    'books', coalesce((select jsonb_agg(b order by b.id desc) from (
        select rb.id, rb.title, rb.author, rb.link, rb.duration_sec, rb.size_mb, rb.created, rb.audience from public.reader_books rb
         where rb.status = 'ready' and rb.audience in ('both', me) order by rb.id desc limit 30) b), '[]'::jsonb));
end $fn$;

-- 5. Права: лише service_role (Edge Function)
revoke all on function public.reader_voice_auth(text, text) from public, anon, authenticated;
revoke all on function public.reader_stale_jobs(int) from public, anon, authenticated;
revoke all on function public.reader_queue_kick() from public, anon, authenticated;
revoke all on function public.reader_voice_list(text) from public, anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_voice_auth(text, text), public.reader_stale_jobs(int), public.reader_voice_list(text) to service_role;
  end if;
end $$;
