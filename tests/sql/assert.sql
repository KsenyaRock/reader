\set ON_ERROR_STOP on
do $$
declare k text; res jsonb; m text; n int;
begin
  select upload_key into k from public.reader_settings;
  if length(k) < 40 then raise exception 'ключ програми закороткий'; end if;

  -- неправильний ключ відхиляється
  begin
    perform public.reader_book_ready('wrong', 'X', 'A', 'https://a/b', 1, 1);
    raise exception 'неправильний ключ пройшов';
  exception when insufficient_privilege then null; end;
  -- посилання не https відхиляється
  begin
    perform public.reader_book_ready(k, 'X', 'A', 'http://a/b', 1, 1);
    raise exception 'http-посилання пройшло';
  exception when invalid_parameter_value then null; end;
  select count(*) into n from public.reader_books;
  if n <> 0 then raise exception 'зайві книги: %', n; end if;

  -- правильний виклик пише книгу й одне повідомлення (Анна вимкнула readerDone)
  res := public.reader_book_ready(k, 'Чорти', 'Джо Аберкромбі', 'https://pub-x.r2.dev/t/h.audiobook.zip', 79000, 516);
  if res <> '{"ok": true}'::jsonb then raise exception 'відповідь: %', res; end if;
  select count(*) into n from public.sent;
  if n <> 1 then raise exception 'повідомлень: %', n; end if;
  select msg into m from public.sent;
  if m not like '%Чорти%' or m not like '%Джо Аберкромбі · 21 год 56 хв · 516 МБ%' or m not like '%https://pub-x.r2.dev/t/h.audiobook.zip%' then
    raise exception 'текст: %', m;
  end if;

  -- повторне оновлення рядка нового повідомлення не дає
  update public.reader_books set size_mb = 517;
  select count(*) into n from public.sent;
  if n <> 1 then raise exception 'дубль повідомлення: %', n; end if;
end $$;

-- права: клієнти не бачать таблиць, але можуть викликати функцію
set role anon;
do $$ begin
  begin perform 1 from public.reader_settings; raise exception 'anon читає reader_settings';
  exception when insufficient_privilege then null; end;
  begin perform 1 from public.reader_books; raise exception 'anon читає reader_books';
  exception when insufficient_privilege then null; end;
  begin perform public.reader_book_ready('wrong', 'X', 'A', 'https://a/b', 1, 1); raise exception 'пройшло';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
select 'SQL читалки: усе гаразд' as result;
