\set ON_ERROR_STOP on
-- крок 21: черга озвучки (до 5 книг чекають, одна йде)
update public.reader_jobs set status = 'failed', error = 'очистка' where status in ('queued', 'running');
update public.reader_jobs set updated = now() - interval '1 hour' where error = 'Скасовано';   -- пауза після скасування з попередніх тестів не заважає
do $$
declare k text; a bigint; b bigint; c bigint; d bigint; e bigint; f bigint; g bigint; cl jsonb; r text; j jsonb; n int;
begin
  select upload_key into k from public.reader_settings;

  -- порожня черга: нічого брати
  if public.reader_queue_claim() is not null then raise exception 'claim з порожньої черги щось повернув'; end if;
  if public.reader_queue_busy() then raise exception 'busy без задач'; end if;

  -- перша книга чекає, claim бере її й віддає дані для запуску
  a := public.reader_job_enqueue('Перша', 'apple', 'https://pub-x.r2.dev/in/q/a.epub', 'both');
  if (select dispatched from public.reader_jobs where id = a) is not null then raise exception 'нова книга вже позначена запущеною'; end if;
  cl := public.reader_queue_claim();
  if (cl->>'id')::bigint is distinct from a or cl->>'gender' <> 'apple' or cl->>'epub_url' <> 'https://pub-x.r2.dev/in/q/a.epub' then raise exception 'claim: %', cl; end if;
  if not public.reader_queue_busy() then raise exception 'після claim має бути зайнято'; end if;

  -- поки перша йде, інші чекають і не запускаються
  b := public.reader_job_enqueue('Друга', 'female', 'https://pub-x.r2.dev/in/q/b.epub', 'both');
  c := public.reader_job_enqueue('Третя', 'male', 'https://pub-x.r2.dev/in/q/c.epub', 'both');
  if public.reader_queue_claim() is not null then raise exception 'друга запущена, поки йде перша'; end if;
  -- читалка бачить чергу як «queued» (так вона показує «у черзі» і кнопку «Скасувати»)
  j := public.reader_voice_list();
  if (select count(*) from jsonb_array_elements(j->'jobs') x where x->>'status' = 'queued') <> 3 then raise exception 'список: %', j; end if;
  -- стара функція створення, як і раніше, відмовляє, поки є активна
  begin perform public.reader_job_create('Стара', 'female', 'https://pub-x.r2.dev/in/q/old.epub'); raise exception 'стара функція пройшла';
  exception when sqlstate '55006' then null; end;

  -- перша стала running, потім done; наступна береться за порядком
  perform public.reader_job_update(k, a, 'running');
  if public.reader_queue_claim() is not null then raise exception 'друга запущена, поки перша running'; end if;
  perform public.reader_job_update(k, a, 'done');
  cl := public.reader_queue_claim();
  if (cl->>'id')::bigint is distinct from b then raise exception 'мала бути друга, а: %', cl; end if;
  if public.reader_queue_claim() is not null then raise exception 'третя запущена одночасно з другою'; end if;

  -- збій теж звільняє чергу
  perform public.reader_job_fail(b, 'GitHub не прийняв запуск (422)');
  cl := public.reader_queue_claim();
  if (cl->>'id')::bigint is distinct from c then raise exception 'мала бути третя, а: %', cl; end if;

  -- скасування книги, що чекає: GitHub не чіпати, нічого не запускається і не ламається активна
  d := public.reader_job_enqueue('Четверта', 'female', 'https://pub-x.r2.dev/in/q/d.epub', 'both');
  r := public.reader_job_cancel2(d);
  if r is distinct from 'waiting' then raise exception 'cancel2 waiting: %', r; end if;
  if (select error from public.reader_jobs where id = d) <> 'Скасовано' or (select status from public.reader_jobs where id = d) <> 'failed' then raise exception 'четверта не скасована'; end if;
  if public.reader_job_cancel2(d) is not null then raise exception 'повторне скасування має повернути null'; end if;
  if public.reader_job_cancel2(999999) is not null then raise exception 'скасування неіснуючої'; end if;
  if (select status from public.reader_jobs where id = c) <> 'queued' then raise exception 'активна третя зачеплена'; end if;

  -- скасування активної: 'active'; одразу після нього claim мовчить 20 с, потім бере наступну
  e := public.reader_job_enqueue('П''ята', 'female', 'https://pub-x.r2.dev/in/q/e.epub', 'both');
  r := public.reader_job_cancel2(c);
  if r is distinct from 'active' then raise exception 'cancel2 active: %', r; end if;
  if public.reader_queue_claim() is not null then raise exception 'claim одразу після скасування активної'; end if;
  update public.reader_jobs set updated = now() - interval '1 minute' where error = 'Скасовано';
  cl := public.reader_queue_claim();
  if (cl->>'id')::bigint is distinct from e then raise exception 'після паузи мала бути п''ята, а: %', cl; end if;
  perform public.reader_job_update(k, e, 'done');

  -- ліміт: 5 книг чекають, шоста відхиляється (код 55007), після звільнення місця знову можна
  for n in 1..5 loop
    perform public.reader_job_enqueue('Ч' || n, 'female', 'https://pub-x.r2.dev/in/q/z' || n || '.epub', 'both');
  end loop;
  begin perform public.reader_job_enqueue('Зайва', 'female', 'https://pub-x.r2.dev/in/q/zz.epub', 'both'); raise exception 'шоста пройшла';
  exception when sqlstate '55007' then null; end;
  cl := public.reader_queue_claim();       -- одна з п'яти стає активною й більше не рахується як та, що чекає
  perform public.reader_job_enqueue('Шоста', 'female', 'https://pub-x.r2.dev/in/q/zz.epub', 'both');
  update public.reader_jobs set status = 'failed', error = 'очистка' where status = 'queued' and dispatched is null;
  perform public.reader_job_update(k, (cl->>'id')::bigint, 'done');

  -- перевірки вхідних даних
  begin perform public.reader_job_enqueue('X', 'female', 'http://a/b', 'both'); raise exception 'http пройшов';
  exception when invalid_parameter_value then null; end;
  begin perform public.reader_job_enqueue('X', 'female', 'https://a/b', 'nobody'); raise exception 'невідомий адресат пройшов';
  exception when invalid_parameter_value then null; end;
  f := public.reader_job_enqueue('Робот', 'robot; drop', 'https://pub-x.r2.dev/in/q/r.epub', 'both');
  if (select gender from public.reader_jobs where id = f) is distinct from 'female' then
    raise exception 'невідомий голос має стати жіночим';
  end if;
end $$;

-- адресат готової книги береться від озвучки, що йде, а не від книги, що чекає
update public.reader_jobs set status = 'failed', error = 'очистка' where status in ('queued', 'running');
update public.reader_jobs set updated = now() - interval '1 hour' where error = 'Скасовано';
insert into public.reader_people (key, label, tg_name, is_admin) values ('anton', 'Олег', 'Олег', true), ('ksenia', 'Анна', 'Анна', false) on conflict (key) do nothing;
do $$
declare k text; a bigint; b bigint; cl jsonb;
begin
  select upload_key into k from public.reader_settings;
  a := public.reader_job_enqueue('Йде для Анни', 'female', 'https://pub-x.r2.dev/in/q/aud1.epub', 'ksenia');
  cl := public.reader_queue_claim();
  b := public.reader_job_enqueue('Чекає для Олега', 'female', 'https://pub-x.r2.dev/in/q/aud2.epub', 'anton');
  perform public.reader_book_ready(k, 'Книга зі черги', 'А', 'https://x.r2.dev/q.zip', 60, 5);
  if (select audience from public.reader_books where title = 'Книга зі черги') is distinct from 'ksenia' then
    raise exception 'книга успадкувала адресата не від тієї задачі: %', (select audience from public.reader_books where title = 'Книга зі черги');
  end if;
  perform public.reader_job_update(k, a, 'done');
  -- завершення викликає штовхач (без pg_net у тесті він лише пише повідомлення), чергу не ламає
  cl := public.reader_queue_claim();
  if (cl->>'id')::bigint is distinct from b then raise exception 'після done має братися книга Олега: %', cl; end if;
end $$;

-- права: anon і authenticated не бачать службових функцій черги, service_role бачить потрібні
set role anon;
do $$ begin
  begin perform public.reader_job_enqueue('X', 'female', 'https://a/b', 'both'); raise exception 'anon ставить у чергу';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_queue_claim(); raise exception 'anon бере з черги';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_job_cancel2(1); raise exception 'anon скасовує';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_queue_kick(); raise exception 'anon штовхає чергу';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
set role service_role;
do $$ begin perform public.reader_queue_claim(); perform public.reader_job_cancel2(999999); end $$;
reset role;
