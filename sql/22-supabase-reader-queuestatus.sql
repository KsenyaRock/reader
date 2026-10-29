-- Читалка: зведений рядок «зараз озвучується…» у списку озвучок. Запускати в Supabase -> SQL Editor -> New query -> Run.
-- Можна запускати повторно. Потрібні скрипти 20 і 21. Сайт (Netlify) не змінюється: читалка просто показує ще один рядок.
-- Якщо йде озвучка, яку цей користувач не бачить (чужа «тільки для…»), у список додається рядок без назви книги:
--   «Зараз озвучується: Ксенія, голос Edge · у черзі всього: 3 · орієнтовно до 14:40».
-- Час оцінюється за тривалістю останніх готових озвучок цим голосом (мінімум 20 хв на книгу), тож він приблизний.
-- Рядок має id 0: кнопка «Скасувати» біля нього нічого не скасує (сервер відповість «Некоректна задача»).
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
     where rj.created > now() - interval '2 days' and rj.status <> 'done' and rj.audience in ('both', me)
     order by rj.id desc limit 10) j;

  for r in select id, gender, status, audience, created, dispatched from public.reader_jobs
            where status in ('queued', 'running') and created > now() - interval '2 days'
              and (dispatched is null or dispatched > now() - interval '6 hours')
            order by (dispatched is null), id loop
    n := n + 1;
    select greatest(20, coalesce(percentile_cont(0.5) within group (order by extract(epoch from d.updated - d.created) / 60), 0))
      into est from (select created, updated from public.reader_jobs
                      where status = 'done' and gender = r.gender order by id desc limit 10) d;
    est := coalesce(est, 20);
    if est = 20 and not exists (select 1 from public.reader_jobs where status = 'done' and gender = r.gender) then
      est := case when r.gender = 'apple' then 90 else 45 end;
    end if;
    total := total + case when r.dispatched is null then est
                          else greatest(5, est - extract(epoch from now() - r.dispatched) / 60) end;
    if r.audience not in ('both', me) and (not h_seen or (r.dispatched is not null and not h_running)) then
      h_seen := true; h_running := r.dispatched is not null; h_key := r.audience;
      h_voice := case when r.gender = 'apple' then 'Apple' else 'Edge' end;
    end if;
  end loop;

  if h_seen then
    ttl := case when h_running then 'Зараз озвучується: ' else 'У черзі: ' end
          || coalesce((select label from public.reader_people where key = h_key), h_key) || ', голос ' || h_voice
          || ' · у черзі всього: ' || n
          || ' · орієнтовно до ' || to_char((now() + make_interval(mins => ceil(total)::int)) at time zone tz, 'HH24:MI');
    sumrow := jsonb_build_object('id', 0, 'title', ttl, 'status', 'running', 'error', null, 'created', now(), 'audience', 'both');
    jobs := jsonb_build_array(sumrow) || jobs;
  end if;

  return jsonb_build_object(
    'people', coalesce((select jsonb_agg(jsonb_build_object('key', key, 'label', label) order by key) from public.reader_people), '[]'::jsonb),
    'jobs', jobs,
    'books', coalesce((select jsonb_agg(b order by b.id desc) from (
        select rb.id, rb.title, rb.author, rb.link, rb.duration_sec, rb.size_mb, rb.created, rb.audience from public.reader_books rb
         where rb.status = 'ready' and rb.audience in ('both', me) order by rb.id desc limit 30) b), '[]'::jsonb));
end $fn$;
revoke all on function public.reader_voice_list(text) from public, anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_voice_list(text) to service_role;
  end if;
end $$;
