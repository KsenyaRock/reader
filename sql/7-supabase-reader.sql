-- Читалка: «Книга готова» + повідомлення в Telegram. Запускати в Supabase -> SQL Editor -> New query -> Run.
-- Можна запускати повторно. Чужі таблиці й функції (tx, todo, tg_*, tuya_settings) не змінюються.
-- Потрібні функції tg_send, tg_targets, tg_pref (скрипт 4).

-- 1. Налаштування: «ключ програми» створюється випадково, показати його: select upload_key from public.reader_settings;
create table if not exists public.reader_settings (
  id int primary key default 1 check (id = 1),
  upload_key text not null default replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '')
);
insert into public.reader_settings (id) values (1) on conflict (id) do nothing;

-- 2. Готові книги
create table if not exists public.reader_books (
  id bigserial primary key,
  title text not null,
  author text,
  link text not null,
  duration_sec int,
  size_mb int,
  status text not null default 'ready',
  created timestamptz not null default now()
);

alter table public.reader_settings enable row level security;
alter table public.reader_books enable row level security;
revoke all on public.reader_settings from anon, authenticated;
revoke all on public.reader_books from anon, authenticated;
revoke all on sequence public.reader_books_id_seq from anon, authenticated;

-- 3. Сповіщення: тригер на нову книгу
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
      || E'\n\nУ читалці: «+» → «Завантажити за посиланням».';
    for r in select * from public.tg_targets() t where public.tg_pref(t.prefs, 'readerDone', true) loop
      perform public.tg_send(r.chat_id, msg);
    end loop;
  end if;
  return new;
exception when others then
  raise notice 'reader_notify_ready: %', sqlerrm;   -- збій сповіщення не має ламати запис книги
  return new;
end $fn$;
revoke all on function public.reader_notify_ready() from public, anon, authenticated;

drop trigger if exists reader_books_ready on public.reader_books;
create trigger reader_books_ready after insert or update on public.reader_books
  for each row execute function public.reader_notify_ready();

-- 4. Виклик із програми озвучки (anon-ключ + «ключ програми»)
create or replace function public.reader_book_ready(k text, p_title text, p_author text, p_link text, p_dur int, p_mb int)
returns jsonb language plpgsql security definer set search_path = public, extensions as $fn$
declare saved text;
begin
  select upload_key into saved from public.reader_settings where id = 1;
  if saved is null or k is null or k <> saved then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  if p_link is null or p_link !~ '^https://' or length(p_link) > 500 then
    raise exception 'bad link' using errcode = '22023';
  end if;
  insert into public.reader_books (title, author, link, duration_sec, size_mb)
  values (left(coalesce(p_title, ''), 300), left(p_author, 300), p_link, p_dur, p_mb);
  return jsonb_build_object('ok', true);
end $fn$;
revoke all on function public.reader_book_ready(text, text, text, text, int, int) from public;
grant execute on function public.reader_book_ready(text, text, text, text, int, int) to anon, authenticated;
