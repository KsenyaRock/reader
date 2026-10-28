\set ON_ERROR_STOP on
-- крок 20: «для кого книга» і сповіщення. Заглушка бота: обидва люди хочуть повідомлення про готові книги
create or replace function public.tg_targets() returns table(uid uuid, chat_id text, name text, prefs jsonb) language sql as $$
  select gen_random_uuid(), '111', 'Олег', '{}'::jsonb union all select gen_random_uuid(), '222', 'Анна', '{}'::jsonb $$;
truncate public.sent;
update public.reader_jobs set status = 'failed', error = 'очистка' where status in ('queued', 'running');
truncate public.sent;
do $$
declare k text; jid bigint; n int; j jsonb; chats text;
begin
  select upload_key into k from public.reader_settings;

  -- без налаштованих людей усе як раніше: книга спільна, повідомлення обом, «приватний» вибір відхиляється
  begin perform public.reader_job_create('X', 'female', 'https://a/b', 'ksenia'); raise exception 'невідомий ключ пройшов';
  exception when invalid_parameter_value then null; end;
  jid := public.reader_job_create('Спільна без людей', 'female', 'https://pub-x.r2.dev/in/p/a.epub');
  if (select audience from public.reader_jobs where id = jid) <> 'both' then raise exception 'за замовчуванням має бути both'; end if;
  perform public.reader_job_fail(jid, 'x');

  insert into public.reader_people (key, label, tg_name, is_admin) values
    ('anton', 'Олег', 'Олег', true), ('ksenia', 'Анна', 'Анна', false)
  on conflict (key) do nothing;
  begin perform public.reader_job_create('X', 'female', 'https://a/b', 'both2'); raise exception 'невідомий ключ пройшов (2)';
  exception when invalid_parameter_value then null; end;

  -- книга лише для Анни: задача, готова книга, повідомлення
  truncate public.sent;
  jid := public.reader_job_create('Для Анни', 'female', 'https://pub-x.r2.dev/in/p/b.epub', 'ksenia');
  if (select audience from public.reader_jobs where id = jid) <> 'ksenia' then raise exception 'для кого не збережено'; end if;
  perform public.reader_book_ready(k, 'Книга Анни', 'А', 'https://x.r2.dev/k.zip', 3600, 100);
  if (select audience from public.reader_books where title = 'Книга Анни') <> 'ksenia' then raise exception 'книга не успадкувала «для кого»'; end if;
  select string_agg(chat, ',' order by chat) into chats from public.sent where msg like '%Книга готова%';
  if chats <> '222' then raise exception 'приватна книга Анни: повідомлення мають піти лише їй, а пішли: %', chats; end if;
  j := public.reader_voice_list('ksenia');     -- поки задача активна, її видно Анни й не видно Олегу
  if not exists (select 1 from jsonb_array_elements(j->'jobs') b where b->>'title' = 'Для Анни') then raise exception 'Анна не бачить свою задачу'; end if;
  if exists (select 1 from jsonb_array_elements(public.reader_voice_list('anton')->'jobs') b where b->>'title' = 'Для Анни') then raise exception 'Олег бачить чужу задачу'; end if;
  perform public.reader_job_update(k, jid, 'done');

  -- видимість у списках
  j := public.reader_voice_list('ksenia');
  if not exists (select 1 from jsonb_array_elements(j->'books') b where b->>'title' = 'Книга Анни') then raise exception 'Анна не бачить свою книгу'; end if;
  j := public.reader_voice_list('anton');
  if exists (select 1 from jsonb_array_elements(j->'books') b where b->>'title' = 'Книга Анни') then raise exception 'Олег бачить чужу книгу'; end if;
  j := public.reader_voice_list();
  if exists (select 1 from jsonb_array_elements(j->'books') b where b->>'title' = 'Книга Анни') then raise exception 'без me видно приватне'; end if;
  if jsonb_array_length(j->'people') <> 2 then raise exception 'люди не віддаються: %', j->'people'; end if;
  if j->'people'->0->>'label' is null or (j->'people'->0) ? 'tg_name' then raise exception 'у списку людей не мають бути службові поля'; end if;

  -- книга лише для Олега йде лише йому
  truncate public.sent;
  jid := public.reader_job_create('Для Олега', 'male', 'https://pub-x.r2.dev/in/p/c.epub', 'anton');
  perform public.reader_book_ready(k, 'Книга Олега', 'А', 'https://x.r2.dev/a.zip', 60, 5);
  select string_agg(chat, ',' order by chat) into chats from public.sent where msg like '%Книга готова%';
  if chats <> '111' then raise exception 'книга Олега: % ', chats; end if;
  perform public.reader_job_update(k, jid, 'done');

  -- спільна книга (озвучка «обом») іде обом; книга з комп'ютера без задачі теж спільна й іде обом
  truncate public.sent;
  jid := public.reader_job_create('Спільна', 'female', 'https://pub-x.r2.dev/in/p/d.epub', 'both');
  perform public.reader_book_ready(k, 'Спільна книга', 'А', 'https://x.r2.dev/s.zip', 60, 5);
  perform public.reader_job_update(k, jid, 'done');
  select string_agg(chat, ',' order by chat) into chats from public.sent where msg like '%Книга готова%';
  if chats <> '111,222' then raise exception 'спільна книга: %', chats; end if;
  truncate public.sent;
  perform public.reader_book_ready(k, 'З комп''ютера', 'А', 'https://x.r2.dev/pc.zip', 60, 5);
  if (select audience from public.reader_books where title = 'З комп''ютера') <> 'both' then raise exception 'книга без задачі має бути спільною'; end if;
  select string_agg(chat, ',' order by chat) into chats from public.sent where msg like '%Книга готова%';
  if chats <> '111,222' then raise exception 'книга з комп''ютера: %', chats; end if;

  -- помилки: завжди й лише адміну, навіть якщо озвучку просила Анна
  truncate public.sent;
  jid := public.reader_job_create('Збій Анни', 'female', 'https://pub-x.r2.dev/in/p/e.epub', 'ksenia');
  perform public.reader_job_update(k, jid, 'failed', 'тестова помилка');
  select string_agg(chat, ',' order by chat) into chats from public.sent where msg like '%Не вдалося озвучити%';
  if chats <> '111' then raise exception 'помилка має йти лише адміну: %', chats; end if;
  if not exists (select 1 from public.sent where msg like '%Для: Анна%') then raise exception 'у помилці не вказано, для кого книга'; end if;

  -- скасування не шле помилку нікому
  truncate public.sent;
  jid := public.reader_job_create('Скасована', 'female', 'https://pub-x.r2.dev/in/p/f.epub', 'ksenia');
  perform public.reader_job_cancel(jid);
  select count(*) into n from public.sent;
  if n <> 0 then raise exception 'скасування дало повідомлення'; end if;
end $$;
set role anon;
do $$ begin
  begin perform public.reader_voice_list('anton'); raise exception 'anon бачить список';
  exception when insufficient_privilege then null; end;
  begin perform 1 from public.reader_people; raise exception 'anon читає людей';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
select 'SQL кроку 20: усе гаразд' as result;
