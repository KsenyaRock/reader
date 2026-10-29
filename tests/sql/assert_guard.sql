\set ON_ERROR_STOP on
-- крок 23: блокування за IP, сторож, список без старих помилок
truncate public.reader_auth_fails;
update public.reader_jobs set status = 'failed', error = 'очистка', updated = now() - interval '5 hours' where status in ('queued', 'running');
do $$
declare code text; j jsonb; a bigint; b bigint; n int;
begin
  select voice_code into code from public.reader_settings;
  -- чужа IP блокується після 5 помилок, інша IP й виклики бази (без IP) працюють
  for i in 1..5 loop
    if public.reader_voice_auth('x' || i, '1.1.1.1') then raise exception 'неправильний код пройшов'; end if;
  end loop;
  begin perform public.reader_voice_auth(code, '1.1.1.1'); raise exception 'IP не заблоковано';
  exception when program_limit_exceeded then null; end;
  if public.reader_voice_auth(code, '2.2.2.2') is not true then raise exception 'інша IP заблокована'; end if;
  if public.reader_voice_auth(code) is not true then raise exception 'виклик бази заблокований'; end if;
  update public.reader_auth_fails set since = now() - interval '16 minutes';
  if public.reader_voice_auth(code, '1.1.1.1') is not true then raise exception 'після паузи IP не працює'; end if;
  select count(*) into n from public.reader_auth_fails where ip = '1.1.1.1';
  if n <> 0 then raise exception 'лічильник IP не скинуто'; end if;

  -- розподілений підбір: 12 IP по 5 помилок = 60, блокує всіх, крім викликів бази
  truncate public.reader_auth_fails;
  for k in 1..12 loop
    for i in 1..5 loop perform public.reader_voice_auth('bad', '10.0.0.' || k); end loop;
  end loop;
  begin perform public.reader_voice_auth(code, '9.9.9.9'); raise exception 'загальна межа не спрацювала';
  exception when program_limit_exceeded then null; end;
  if public.reader_voice_auth(code) is not true then raise exception 'виклик бази заблокований загальною межею'; end if;
  truncate public.reader_auth_fails;

  -- сторож: запущені давно потрапляють у список
  a := public.reader_job_enqueue('Стара', 'apple', 'https://pub-x.r2.dev/in/s/s1.epub', 'both');
  perform public.reader_queue_claim();
  if jsonb_array_length(public.reader_stale_jobs(10)) <> 0 then raise exception 'щойно запущена вважається завислою'; end if;
  update public.reader_jobs set dispatched = now() - interval '11 minutes' where id = a;
  j := public.reader_stale_jobs(10);
  if j->0->>'id' <> a::text or j->0->>'gender' <> 'apple' then raise exception 'сторож: %', j; end if;
  perform public.reader_job_fail(a, 'Запуск завершився без результату');
  if jsonb_array_length(public.reader_stale_jobs(10)) <> 0 then raise exception 'позначена задача лишилась у списку'; end if;

  -- список: свіжа помилка видна, стара й «Скасовано» ні
  b := public.reader_job_enqueue('Скасована', 'female', 'https://pub-x.r2.dev/in/s/s2.epub', 'both');
  perform public.reader_job_cancel2(b);
  j := public.reader_voice_list('anton');
  if not exists (select 1 from jsonb_array_elements(j->'jobs') x where (x->>'id')::bigint = a) then raise exception 'свіжа помилка зникла'; end if;
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where (x->>'id')::bigint = b) then raise exception '«Скасовано» показується'; end if;
  update public.reader_jobs set updated = now() - interval '4 hours' where id = a;
  j := public.reader_voice_list('anton');
  if exists (select 1 from jsonb_array_elements(j->'jobs') x where (x->>'id')::bigint = a) then raise exception 'стара помилка показується'; end if;

  -- anon не бачить нових функцій
  begin set local role anon; perform public.reader_stale_jobs(10); reset role; raise exception 'anon викликає reader_stale_jobs';
  exception when insufficient_privilege then reset role; end;
end $$;
