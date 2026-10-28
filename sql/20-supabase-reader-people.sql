-- Читалка, крок 20: «для кого книга» (лише мені, лише іншому, обом) і розумні сповіщення.
-- Безпечно запускати повторно. Чіпає лише reader_*. Потрібні скрипти 7, 8, 9, 18, 19.
-- Після цього скрипта один раз внесіть людей (див. кінець файлу або підказку в чаті): без цього все працює як раніше (книги спільні).

-- 1. Люди читалки: ключ, підпис у кнопках, ім'я в Telegram-боті (public.tg_targets().name), чи отримує помилки озвучки
create table if not exists public.reader_people (
  key text primary key check (key ~ '^[a-z0-9_]{1,20}$' and key <> 'both'),
  label text not null,
  tg_name text not null,
  is_admin boolean not null default false
);
alter table public.reader_people enable row level security;
revoke all on public.reader_people from anon, authenticated;

-- 2. «Для кого» в задачах і книгах: both = спільна (усі наявні книги лишаються спільними)
alter table public.reader_jobs add column if not exists audience text not null default 'both';
alter table public.reader_books add column if not exists audience text not null default 'both';

-- 3. Нова задача з вибором «для кого». Невідомий ключ це помилка (щоб приватна книга ніколи не стала спільною через друкарську помилку)
drop function if exists public.reader_job_create(text, text, text);
create or replace function public.reader_job_create(p_title text, p_gender text, p_url text, p_for text default 'both') returns bigint
language plpgsql security definer set search_path = public, extensions as $fn$
declare nid bigint; aud text := coalesce(nullif(p_for, ''), 'both');
begin
  if p_url is null or p_url !~ '^https://' or length(p_url) > 500 then
    raise exception 'bad url' using errcode = '22023';
  end if;
  if aud <> 'both' and not exists (select 1 from public.reader_people where key = aud) then
    raise exception 'bad audience' using errcode = '22023';
  end if;
  if exists (select 1 from public.reader_jobs where status in ('queued', 'running') and created > now() - interval '6 hours') then
    raise exception 'busy' using errcode = '55006';
  end if;
  insert into public.reader_jobs (title, gender, epub_url, audience)
  values (left(coalesce(nullif(p_title, ''), 'без назви'), 300),
          case when p_gender in ('male', 'apple') then p_gender else 'female' end, p_url, aud)
  returning id into nid;
  return nid;
end $fn$;

-- 4. Готова книга успадковує «для кого» від активної озвучки (активна завжди одна). Книга з комп'ютера без задачі лишається спільною
create or replace function public.reader_book_audience() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
declare a text;
begin
  if new.audience is null or new.audience = 'both' then
    select audience into a from public.reader_jobs
     where status in ('queued', 'running') and created > now() - interval '6 hours' order by id desc limit 1;
    new.audience := coalesce(a, 'both');
  end if;
  return new;
end $fn$;
revoke all on function public.reader_book_audience() from public, anon, authenticated;
drop trigger if exists reader_books_audience on public.reader_books;
create trigger reader_books_audience before insert on public.reader_books
  for each row execute function public.reader_book_audience();

-- 5. Список для читалки: лише спільні та мої; p_me порожній = лише спільні. Разом віддає людей для кнопок
drop function if exists public.reader_voice_list();
create or replace function public.reader_voice_list(p_me text default null) returns jsonb
language sql security definer set search_path = public, extensions as $fn$
  select jsonb_build_object(
    'people', coalesce((select jsonb_agg(jsonb_build_object('key', key, 'label', label) order by key) from public.reader_people), '[]'::jsonb),
    'jobs', coalesce((select jsonb_agg(j order by j.id desc) from (
        select id, title, status, error, created, audience from public.reader_jobs
         where created > now() - interval '2 days' and status <> 'done' and audience in ('both', coalesce(p_me, 'both'))
         order by id desc limit 10) j), '[]'::jsonb),
    'books', coalesce((select jsonb_agg(b order by b.id desc) from (
        select id, title, author, link, duration_sec, size_mb, created, audience from public.reader_books
         where status = 'ready' and audience in ('both', coalesce(p_me, 'both')) order by id desc limit 30) b), '[]'::jsonb));
$fn$;

-- 6. Telegram: «Книга готова» лише тому, кому призначена (спільна обом)
create or replace function public.reader_notify_ready() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
declare r record; msg text; d text := '';
begin
  if new.status = 'ready' and (tg_op = 'INSERT' or old.status is distinct from 'ready') then
    if new.duration_sec is not null and new.duration_sec > 0 then
      d := case when new.duration_sec >= 3600 then (new.duration_sec / 3600) || ' год ' else '' end
           || ((new.duration_sec % 3600) / 60) || ' хв';
    end if;
    msg := '📚 Книга готова: ' || coalesce(nullif(new.title, ''), 'без назви')
      || E'\n' || concat_ws(' · ', nullif(new.author, ''), nullif(d, ''),
           case when new.size_mb is not null then new.size_mb || ' МБ' end)
      || E'\n\nПосилання:\n' || new.link
      || E'\n\nУ читалці: «+» → «Додати» біля книги.';
    for r in select * from public.tg_targets() t
              where public.tg_pref(t.prefs, 'readerDone', true)
                and (new.audience = 'both'
                     or t.name in (select pp.tg_name from public.reader_people pp where pp.key = new.audience)) loop
      perform public.tg_send(r.chat_id, msg);
    end loop;
  end if;
  return new;
exception when others then
  raise notice 'reader_notify_ready: %', sqlerrm;
  return new;
end $fn$;
revoke all on function public.reader_notify_ready() from public, anon, authenticated;

-- 7. Telegram: помилки озвучки завжди й лише адміну (без налаштованих людей, як раніше, усім)
create or replace function public.reader_notify_failed() returns trigger
language plpgsql security definer set search_path = public, extensions as $fn$
declare r record; msg text; has_admin boolean := exists (select 1 from public.reader_people where is_admin);
begin
  if new.status = 'failed' and old.status is distinct from 'failed' and new.error is distinct from 'Скасовано' then
    msg := '❌ Не вдалося озвучити: ' || new.title || coalesce(E'\nПричина: ' || nullif(new.error, ''), '')
      || coalesce(E'\nДля: ' || (select pp.label from public.reader_people pp where pp.key = new.audience), '')
      || E'\n\nМожна спробувати ще раз у читалці: «+» → «Озвучити книгу».';
    for r in select * from public.tg_targets() t
              where case when has_admin then t.name in (select pp.tg_name from public.reader_people pp where pp.is_admin)
                         else public.tg_pref(t.prefs, 'readerDone', true) end loop
      perform public.tg_send(r.chat_id, msg);
    end loop;
  end if;
  return new;
exception when others then
  raise notice 'reader_notify_failed: %', sqlerrm;
  return new;
end $fn$;
revoke all on function public.reader_notify_failed() from public, anon, authenticated;

-- 8. Права: лише service_role (Edge Function)
revoke all on function public.reader_job_create(text, text, text, text) from public, anon, authenticated;
revoke all on function public.reader_voice_list(text) from public, anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_job_create(text, text, text, text), public.reader_voice_list(text) to service_role;
  end if;
end $$;

-- 9. Люди (виконати один раз, імена як у боті; змініть is_admin, якщо помилки мають іти комусь іншому):
-- insert into public.reader_people (key, label, tg_name, is_admin) values
--   ('anton', 'Олег', 'Олег', true),
--   ('ksenia', 'Анна', 'Анна', false)
-- on conflict (key) do update set label = excluded.label, tg_name = excluded.tg_name, is_admin = excluded.is_admin;
