-- Читалка, крок 9: видалення озвучених книг із хмари + справжня назва в списку задач.
-- Безпечно запускати повторно. Чіпає лише reader_* (нові функції).

-- 1. Посилання готової книги (для Edge Function, лише service_role)
create or replace function public.reader_book_link(p_id bigint) returns text
language sql security definer set search_path = public, extensions as $fn$
  select link from public.reader_books where id = p_id;
$fn$;

-- 2. Видалення запису книги (після того, як файл видалено зі сховища)
create or replace function public.reader_book_delete(p_id bigint) returns boolean
language plpgsql security definer set search_path = public, extensions as $fn$
declare n int;
begin
  delete from public.reader_books where id = p_id;
  get diagnostics n = row_count;
  return n > 0;
end $fn$;

-- 3. Справжня назва книги в задачі (викликає хмарна озвучка: anon-ключ + ключ програми)
create or replace function public.reader_job_title(k text, p_job bigint, p_title text) returns jsonb
language plpgsql security definer set search_path = public, extensions as $fn$
declare saved text;
begin
  select upload_key into saved from public.reader_settings where id = 1;
  if saved is null or k is null or k <> saved then
    raise exception 'forbidden' using errcode = '42501';
  end if;
  update public.reader_jobs set title = left(coalesce(nullif(btrim(p_title), ''), title), 200), updated = now() where id = p_job;
  return jsonb_build_object('ok', true);
end $fn$;

revoke all on function public.reader_book_link(bigint) from public, anon, authenticated;
revoke all on function public.reader_book_delete(bigint) from public, anon, authenticated;
revoke all on function public.reader_job_title(text, bigint, text) from public;
grant execute on function public.reader_job_title(text, bigint, text) to anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_book_link(bigint), public.reader_book_delete(bigint) to service_role;
  end if;
end $$;
