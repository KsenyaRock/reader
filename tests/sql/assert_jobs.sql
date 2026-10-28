\set ON_ERROR_STOP on
truncate public.sent;
do $$
declare code text; k text; id1 bigint; n int; m text; ok boolean; j jsonb;
begin
  select voice_code, upload_key into code, k from public.reader_settings;
  if length(code) <> 12 then raise exception 'код запуску: %', code; end if;

  -- перевірка коду з обмеженням спроб
  if public.reader_voice_auth(code) is not true then raise exception 'правильний код не пройшов'; end if;
  for i in 1..5 loop
    if public.reader_voice_auth('wrong' || i) then raise exception 'неправильний код пройшов'; end if;
  end loop;
  begin
    perform public.reader_voice_auth(code);
    raise exception 'блокування після 5 помилок не спрацювало';
  exception when program_limit_exceeded then null; end;
  update public.reader_settings set fail_since = now() - interval '16 minutes';
  if public.reader_voice_auth(code) is not true then raise exception 'після паузи код не працює'; end if;
  select fail_count into n from public.reader_settings;
  if n <> 0 then raise exception 'лічильник не скинуто: %', n; end if;

  -- створення задачі: https обов'язково, одна активна за раз
  begin perform public.reader_job_create('X', 'female', 'http://a/b'); raise exception 'http пройшов';
  exception when invalid_parameter_value then null; end;
  id1 := public.reader_job_create('Книга', 'male', 'https://pub-x.r2.dev/in/t/b.epub');
  begin perform public.reader_job_create('Друга', 'female', 'https://pub-x.r2.dev/in/t/c.epub'); raise exception 'друга активна пройшла';
  exception when object_not_in_prerequisite_state then null; end;
  select gender into m from public.reader_jobs where id = id1;
  if m <> 'male' then raise exception 'голос: %', m; end if;

  -- оновлення з хмарної задачі: ключ, статуси
  begin perform public.reader_job_update('wrong', id1, 'running'); raise exception 'неправильний ключ пройшов';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_job_update(k, id1, 'weird'); raise exception 'поганий статус пройшов';
  exception when invalid_parameter_value then null; end;
  perform public.reader_job_update(k, id1, 'running');
  select status into m from public.reader_jobs where id = id1;
  if m <> 'running' then raise exception 'статус: %', m; end if;
  select count(*) into n from public.sent; if n <> 0 then raise exception 'зайві повідомлення: %', n; end if;

  -- помилка: одне повідомлення (Анна вимкнула), повторна помилка нічого не шле
  perform public.reader_job_update(k, id1, 'failed', 'Edge TTS недоступний');
  select count(*) into n from public.sent; if n <> 1 then raise exception 'повідомлень про помилку: %', n; end if;
  select msg into m from public.sent;
  if m not like '❌ Не вдалося озвучити: Книга%' or m not like '%Edge TTS недоступний%' then raise exception 'текст: %', m; end if;
  perform public.reader_job_update(k, id1, 'failed', 'ще раз');
  select count(*) into n from public.sent; if n <> 1 then raise exception 'дубль помилки: %', n; end if;

  -- після помилки можна створити нову; done не повертається назад у failed
  id1 := public.reader_job_create('Третя', 'female', 'https://pub-x.r2.dev/in/t/d.epub');
  perform public.reader_job_update(k, id1, 'done');
  perform public.reader_job_update(k, id1, 'failed', 'пізно');
  select status into m from public.reader_jobs where id = id1;
  if m <> 'done' then raise exception 'done змінено: %', m; end if;

  -- список для читалки: активні/помилкові задачі й готові книги
  perform public.reader_book_ready(k, 'Чорти', 'Аберкромбі', 'https://pub-x.r2.dev/t/h.audiobook.zip', 79000, 516);
  j := public.reader_voice_list();
  if jsonb_array_length(j -> 'books') < 1 or (j -> 'books' -> 0 ->> 'link') is null then raise exception 'список книг: %', j; end if;
  if jsonb_array_length(j -> 'jobs') <> 1 or (j -> 'jobs' -> 0 ->> 'status') <> 'failed' then raise exception 'список задач: %', j; end if;
end $$;

-- права: anon не бачить таблиць і службових функцій, але може оновлювати статус з ключем
set role anon;
do $$ begin
  begin perform 1 from public.reader_jobs; raise exception 'anon читає reader_jobs';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_voice_auth('x'); raise exception 'anon викликає reader_voice_auth';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_job_create('X', 'female', 'https://a/b'); raise exception 'anon створює задачу';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_voice_list(); raise exception 'anon бачить список';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_job_update('wrong', 1, 'running'); raise exception 'пройшло';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
set role service_role;
do $$ begin perform public.reader_voice_list(); end $$;
reset role;
select 'SQL озвучки з телефона: усе гаразд' as result;

-- крок 9: назва задачі й видалення книги
do $$
declare k text; jid bigint; bid bigint; t text; l text; ok boolean;
begin
  select upload_key into k from public.reader_settings where id = 1;
  jid := public.reader_job_create('book.epub', 'female', 'https://pub-x.r2.dev/in/x/book.epub');
  perform public.reader_job_title(k, jid, '  Чорти  ');
  select title into t from public.reader_jobs where id = jid;
  if t <> 'Чорти' then raise exception 'назва задачі: %', t; end if;
  perform public.reader_job_title(k, jid, '   ');
  select title into t from public.reader_jobs where id = jid;
  if t <> 'Чорти' then raise exception 'порожня назва стерла: %', t; end if;
  begin perform public.reader_job_title('wrong', jid, 'X'); raise exception 'пройшло';
  exception when insufficient_privilege then null; end;
  perform public.reader_job_update(k, jid, 'done');

  select id into bid from public.reader_books order by id desc limit 1;
  l := public.reader_book_link(bid);
  if l is null or l not like 'https://%' then raise exception 'посилання: %', l; end if;
  if public.reader_book_link(-5) is not null then raise exception 'посилання неіснуючої книги'; end if;
  ok := public.reader_book_delete(bid);
  if not ok or exists (select 1 from public.reader_books where id = bid) then raise exception 'книгу не видалено'; end if;
  if public.reader_book_delete(bid) then raise exception 'повторне видалення мало б повернути false'; end if;
end $$;
set role anon;
do $$ begin
  begin perform public.reader_book_link(1); raise exception 'anon бачить посилання';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_book_delete(1); raise exception 'anon видаляє книгу';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
select 'SQL кроку 9: усе гаразд' as result;

-- Крок 18: голос Apple
do $$ declare jid bigint; g text; begin
  update public.reader_jobs set status = 'done' where status in ('queued', 'running');
  jid := public.reader_job_create('Леся', 'apple', 'https://pub-x.r2.dev/in/a/book.epub');
  select gender into g from public.reader_jobs where id = jid;
  if g <> 'apple' then raise exception 'голос apple не збережено: %', g; end if;
  update public.reader_jobs set status = 'done' where id = jid;
  jid := public.reader_job_create('Зла', 'robot; drop', 'https://pub-x.r2.dev/in/a/c.epub');
  select gender into g from public.reader_jobs where id = jid;
  if g <> 'female' then raise exception 'невідомий голос має ставати female: %', g; end if;
  update public.reader_jobs set status = 'done' where id = jid;
end $$;
set role anon;
do $$ begin
  begin perform public.reader_job_create('X', 'apple', 'https://a/b'); raise exception 'anon створює задачу (крок 18)';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
select 'SQL кроку 18: усе гаразд' as result;

-- Крок 19: скасування озвучки і синхронізація позиції
do $$ declare jid bigint; n int; st text; er text; begin
  update public.reader_jobs set status = 'done' where status in ('queued', 'running');
  delete from public.sent;
  jid := public.reader_job_create('Скасована', 'apple', 'https://pub-x.r2.dev/in/c/book.epub');
  if not public.reader_job_cancel(jid) then raise exception 'cancel не спрацював'; end if;
  select status, error into st, er from public.reader_jobs where id = jid;
  if st <> 'failed' or er <> 'Скасовано' then raise exception 'стан після cancel: % %', st, er; end if;
  select count(*) into n from public.sent;
  if n <> 0 then raise exception 'про скасування не мало бути повідомлення, а є %', n; end if;
  if public.reader_job_cancel(jid) then raise exception 'повторне скасування мало б повернути false'; end if;
  if public.reader_job_cancel(-1) then raise exception 'неіснуюча задача'; end if;
  -- звичайний збій, як і раніше, дає повідомлення, а пізніша відмітка «перервалась» не перезаписує причину скасування
  perform public.reader_job_update((select upload_key from public.reader_settings where id = 1), jid, 'failed', 'Озвучка перервалась');
  select error into er from public.reader_jobs where id = jid;
  if er <> 'Скасовано' then raise exception 'причину скасування перезаписано: %', er; end if;
  jid := public.reader_job_create('Збій', 'female', 'https://pub-x.r2.dev/in/c/d.epub');
  perform public.reader_job_fail(jid, 'GitHub не прийняв');
  select count(*) into n from public.sent;
  if n < 1 then raise exception 'збій мав дати повідомлення'; end if;

  perform public.reader_progress_set('Анна', 'abc1234567', 125.7, 40000);
  if (public.reader_progress_get('Анна', 'abc1234567')->>'pos')::numeric <> 125.7 then raise exception 'позицію не збережено'; end if;
  perform public.reader_progress_set('Анна', 'abc1234567', 300, 40000);
  if (public.reader_progress_get('Анна', 'abc1234567')->>'pos')::numeric <> 300 then raise exception 'позицію не оновлено'; end if;
  if public.reader_progress_get('Олег', 'abc1234567') is not null then raise exception 'профілі мають бути окремі'; end if;
  perform public.reader_progress_set('Анна', 'abc1234567', -5, -9);
  if (public.reader_progress_get('Анна', 'abc1234567')->>'pos')::numeric <> 0 then raise exception 'від''ємна позиція має ставати 0'; end if;
end $$;
set role anon;
do $$ begin
  begin perform public.reader_job_cancel(1); raise exception 'anon скасовує';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_progress_get('Анна', 'abc1234567'); raise exception 'anon читає позицію';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_progress_set('x', 'abcd', 1, 1); raise exception 'anon пише позицію';
  exception when insufficient_privilege then null; end;
  begin perform 1 from public.reader_progress; raise exception 'anon читає таблицю';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
select 'SQL кроку 19: усе гаразд' as result;
