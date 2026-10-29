\set ON_ERROR_STOP on
-- крок 22: зведений рядок про чужу озвучку
update public.reader_jobs set status = 'failed', error = 'очистка' where status in ('queued', 'running');
update public.reader_jobs set updated = now() - interval '1 hour' where error = 'Скасовано';
insert into public.reader_people (key, label, tg_name, is_admin) values ('anton', 'Олег', 'Олег', true), ('ksenia', 'Анна', 'Анна', false) on conflict (key) do nothing;
do $$
declare k text; a bigint; b bigint; j jsonb; t text; cl jsonb;
begin
  select upload_key into k from public.reader_settings;
  -- нічого не йде: рядка немає
  j := public.reader_voice_list('anton');
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where (x->>'id')::bigint = 0) then raise exception 'зайвий зведений рядок: %', j; end if;

  -- йде книга Анни, чекає книга Олега
  a := public.reader_job_enqueue('Секретна книга Анни', 'female', 'https://pub-x.r2.dev/in/s/a.epub', 'ksenia');
  cl := public.reader_queue_claim();
  b := public.reader_job_enqueue('Книга Олега', 'apple', 'https://pub-x.r2.dev/in/s/b.epub', 'anton');

  -- Олег бачить зведений рядок без назви чужої книги
  j := public.reader_voice_list('anton');
  t := j->'jobs'->0->>'title';
  if (j->'jobs'->0->>'id')::bigint <> 0 or t not like 'Зараз озвучується: Анна, голос Edge · у черзі всього: 2 · орієнтовно до __:__' then raise exception 'рядок Олега: %', j; end if;
  if t like '%Секретна%' then raise exception 'назва чужої книги витекла'; end if;
  if (select count(*) from jsonb_array_elements(j->'jobs') x where x->>'title' = 'Книга Олега') <> 1 then raise exception 'власна книга зникла'; end if;
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where x->>'title' = 'Секретна книга Анни') then raise exception 'Олег бачить чужу задачу'; end if;

  -- Анна свою бачить, а чужу (Олега, що чекає) — лише зведеним рядком
  j := public.reader_voice_list('ksenia');
  if not exists (select 1 from jsonb_array_elements(j->'jobs') x where x->>'title' = 'Секретна книга Анни') then raise exception 'Анна не бачить свою'; end if;
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where x->>'title' = 'Книга Олега') then raise exception 'Анна бачить чужу назву'; end if;

  -- без профілю бачить лише спільне; приватна озвучка теж лише рядком
  j := public.reader_voice_list();
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where x->>'title' in ('Секретна книга Анни', 'Книга Олега')) then raise exception 'без профілю видно приватне'; end if;

  -- коли все своє, зведеного рядка немає
  perform public.reader_job_update(k, a, 'done');
  update public.reader_jobs set status = 'failed', error = 'очистка' where id = b;
  perform public.reader_job_enqueue('Моя', 'female', 'https://pub-x.r2.dev/in/s/c.epub', 'anton');
  j := public.reader_voice_list('anton');
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where (x->>'id')::bigint = 0) then raise exception 'зведений рядок для власної: %', j; end if;
end $$;
