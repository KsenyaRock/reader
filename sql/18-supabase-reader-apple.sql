-- Читалка, крок 18: голос Apple (Леся, Мілена, Samantha) для озвучки з телефона.
-- Безпечно запускати повторно. Чіпає лише reader_* (оновлює одну функцію, таблиці не змінює).

-- Нова задача: тепер голос може бути female, male або apple (решта, як і раніше, female)
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
  values (left(coalesce(nullif(p_title, ''), 'без назви'), 300),
          case when p_gender in ('male', 'apple') then p_gender else 'female' end, p_url)
  returning id into nid;
  return nid;
end $fn$;

revoke all on function public.reader_job_create(text, text, text) from public, anon, authenticated;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.reader_job_create(text, text, text) to service_role;
  end if;
end $$;
