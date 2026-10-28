"""Тести програми озвучки: розбір EPUB, мови, нарізка, збірка .audiobook.zip (з заглушкою edge_tts)."""
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tool"))
sys.path.insert(0, os.path.join(HERE, "stub"))
import audiobook_tool as T  # noqa: E402
from helpers import make_epub, page, run_tool  # noqa: E402

WORK = tempfile.mkdtemp()
os.environ["OZVUCHKA_WORK"] = WORK  # робоча тека тестів
T.RETRY_DELAY = 0  # у тестах не чекаємо між повторами

UK = "Це початок нашої історії. Він був у місті, де ніхто не знав його імені, і це було добре."
RU = "Это второй раздел, написанный по-русски. Мы ещё не знали, что будет дальше, и ждали."
EN = "Hello, how are you today? I am fine, thank you very much for asking about it."


def sample_epub(path):
    nav = '<html><body><nav><ol><li>Розділ перший Розділ перший Розділ перший Розділ перший</li></ol></nav></body></html>'
    return make_epub(path, [
        ("cover.xhtml", page("", "Обкладинка")),                                  # закоротко: пропускається
        ("nav.xhtml", nav, "nav"),                                                  # навігація: пропускається
        ("c1.xhtml", page("Розділ перший", UK, "«" + EN + "»", "Просто &amp; ще &lt;текст&gt; для довжини цього абзацу.")),
        ("text/c%202.xhtml", page("Глава вторая", RU, "Съёмки продолжались всю ночь, и никто не ушёл домой.")),
    ])


class ReadEpub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.epub = sample_epub(os.path.join(self.tmp.name, "book.epub"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_and_chapters(self):
        b = T.read_epub(self.epub)
        self.assertEqual((b["title"], b["author"], b["lang"]), ("Тестова книга", "Автор", "uk"))
        self.assertEqual([c["title"] for c in b["chapters"]], ["Розділ перший", "Глава вторая"])

    def test_skips_nav_and_cover_and_styles(self):
        b = T.read_epub(self.epub)
        text = " ".join(" ".join(c["paras"]) for c in b["chapters"])
        self.assertNotIn("color:red", text)
        self.assertNotIn("Обкладинка", text)
        self.assertNotIn("Розділ перший Розділ перший", text)

    def test_entities_and_percent_encoded_href(self):
        b = T.read_epub(self.epub)
        self.assertIn("Просто & ще <текст> для довжини цього абзацу.", b["chapters"][0]["paras"])
        self.assertEqual(len(b["chapters"]), 2)  # другий розділ лежить у text/c%202.xhtml


class SkipJunk(unittest.TestCase):
    LONG = "Це довгий абзац справжнього тексту книги. " * 30

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def read(self, chapters):
        f = io.StringIO()
        with redirect_stdout(f):
            book = T.read_epub(make_epub(os.path.join(self.dir, "b.epub"), chapters))
        return book, f.getvalue()

    def body(self, etype, text, h="Розділ"):
        return f'<html xmlns:epub="http://www.idpf.org/2007/ops"><body epub:type="{etype}"><h1>{h}</h1><p>{text}</p></body></html>'

    def test_skips_marked_toc_and_copyright(self):
        book, log = self.read([
            ("t.xhtml", self.body("toc", "Розділ 1 .... 5. Розділ 2 .... 9. Розділ 3 .... 20", "Зміст")),
            ("c.xhtml", self.body("copyright-page", "Видавництво. Київ. 2020. Усі права збережено.", "Вихідні")),
            ("a.xhtml", page("Початок", self.LONG)), ("b.xhtml", page("Далі", self.LONG)),
            ("d.xhtml", page("Кінець", self.LONG)), ("e.xhtml", page("Ще", self.LONG)),
            ("f.xhtml", page("І ще", self.LONG)), ("g.xhtml", page("Фінал", self.LONG)),
        ])
        self.assertEqual([c["title"] for c in book["chapters"]], ["Початок", "Далі", "Кінець", "Ще", "І ще", "Фінал"])
        self.assertEqual(len(book["skipped"]), 2)
        self.assertIn("ПРОПУЩЕНО", log)

    def test_short_title_trap_only_for_short_chapters(self):
        book, _ = self.read([
            ("t.xhtml", page("Зміст", "Глава перша. Глава друга. Глава третя. Глава четверта.")),
            ("a.xhtml", page("Початок", self.LONG)), ("b.xhtml", page("Далі", self.LONG)),
            ("c.xhtml", page("Contents", self.LONG * 3)),     # довга глава з назвою «Contents» — це текст книги
            ("d.xhtml", page("Кінець", self.LONG)), ("e.xhtml", page("Ще", self.LONG)),
        ])
        titles = [c["title"] for c in book["chapters"]]
        self.assertNotIn("Зміст", titles)
        self.assertIn("Contents", titles)

    def test_isbn_short_page_skipped_but_long_page_with_copyright_sign_kept(self):
        book, _ = self.read([
            ("i.xhtml", page("", "ISBN 978-966-00-0000-0. © Видавництво, 2020.")),
            ("a.xhtml", page("Початок", self.LONG + " © " + self.LONG)),
            ("b.xhtml", page("Далі", self.LONG)), ("c.xhtml", page("Ще", self.LONG)),
            ("d.xhtml", page("Кінець", self.LONG)), ("e.xhtml", page("Фінал", self.LONG)),
        ])
        self.assertEqual(len(book["chapters"]), 5)
        self.assertEqual(len(book["skipped"]), 1)

    def test_safeguard_when_too_much_marked_junk(self):
        book, log = self.read([
            ("n.xhtml", self.body("endnotes", self.LONG * 2, "Примітки")),
            ("a.xhtml", page("Початок", self.LONG)),
        ])
        self.assertEqual(len(book["chapters"]), 2)
        self.assertEqual(book["skipped"], [])
        self.assertIn("нічого не пропускаю", log)

    def test_never_empties_book(self):
        book, _ = self.read([("t.xhtml", self.body("toc", "Розділ 1. Розділ 2. Розділ 3. Розділ 4. Розділ 5.", "Зміст"))])
        self.assertEqual(len(book["chapters"]), 1)

    def test_plain_book_untouched(self):
        book, log = self.read([("a.xhtml", page("Один", self.LONG)), ("b.xhtml", page("Два", self.LONG))])
        self.assertEqual(len(book["chapters"]), 2)
        self.assertEqual(book["skipped"], [])
        self.assertNotIn("ПРОПУЩЕНО", log)


class TextTimeline(unittest.TestCase):
    def test_timeline_follows_chunk_durations(self):
        d = tempfile.mkdtemp()
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "stub"))
        import edge_tts
        chunks = []
        for i, txt in enumerate(["Перший.\n\nДругий абзац довший.", "Третій."]):
            path = os.path.join(d, f"{i}.mp3")
            with open(path, "wb") as f:
                f.write(edge_tts.silent_mp3(4 + 2 * i))
            chunks.append({"path": path, "text": txt})
        tl = T.text_timeline(chunks)
        self.assertEqual([x[1] for x in tl], ["Перший.", "Другий абзац довший.", "Третій."])
        self.assertEqual(tl[0][0], 0.0)
        self.assertTrue(0 < tl[1][0] < tl[2][0])
        self.assertAlmostEqual(tl[2][0], T.mp3_duration(chunks[0]["path"]), delta=0.2)   # новий фрагмент починається там, де скінчився попередній

    def test_pauses_count_in_weight(self):
        self.assertGreater(T.speech_weight("Так. Ні. Може."), T.speech_weight("Такніможе") + 20)
        self.assertGreater(T.speech_weight("Розділ"), len("Розділ"))     # навіть заголовок має паузу після себе

    def test_zip_contains_text_json(self):
        d = tempfile.mkdtemp()
        epub = make_epub(os.path.join(d, "b.epub"), [("a.xhtml", page("Один", "Перший абзац книги. " * 5, "Другий абзац книги. " * 5))])
        r = run_tool(epub, "--out", os.path.join(d, "out"), env_extra={"OZVUCHKA_WORK": os.path.join(d, "w")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        zp = next(os.path.join(d, "out", f) for f in os.listdir(os.path.join(d, "out")) if f.endswith(".audiobook.zip"))
        with zipfile.ZipFile(zp) as z:
            man = json.loads(z.read("manifest.json"))
            tx = json.loads(z.read("text.json"))
        self.assertTrue(man["text"])
        self.assertEqual(tx["v"], 1)
        first = tx["chapters"][man["chapters"][0]["file"]]
        self.assertEqual(len(first), 3)      # заголовок і два абзаци
        self.assertTrue(first[1][1].startswith("Перший абзац"))
        self.assertGreater(first[2][0], first[1][0])


class Languages(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(T.detect_lang(UK), "uk")
        self.assertEqual(T.detect_lang(RU), "ru")
        self.assertEqual(T.detect_lang(EN), "en")
        self.assertEqual(T.detect_lang("Так"), "cyr")
        self.assertIsNone(T.detect_lang("123 — 456"))

    def test_ambiguous_paragraph_takes_next_neighbour(self):
        out = T.label_paragraphs(["Глава", RU, UK], "uk")
        self.assertEqual([l for l, _ in out], ["ru", "ru", "uk"])

    def test_default_language_when_nothing_decides(self):
        self.assertEqual([l for l, _ in T.label_paragraphs(["Так", "Нет"], "ru")], ["ru", "ru"])

    def test_punctuation_added(self):
        self.assertTrue(T.label_paragraphs(["Заголовок"], "uk")[0][1].endswith("."))
        self.assertEqual(T.label_paragraphs(["Питання?"], "uk")[0][1], "Питання?")


class Chunks(unittest.TestCase):
    def test_limit_and_language_switch(self):
        labeled = [("uk", UK)] * 40 + [("en", EN)] * 3 + [("uk", UK)]
        chunks = T.make_chunks(labeled, limit=500)
        self.assertTrue(all(len(c["text"]) <= 500 for c in chunks))
        self.assertEqual([c["lang"] for c in chunks if c["lang"] == "en"], ["en"])
        langs = [c["lang"] for c in chunks]
        self.assertEqual(langs[-2:], ["en", "uk"])  # зміна мови починає новий фрагмент

    def test_long_text_without_punctuation_is_cut(self):
        chunks = T.make_chunks([("uk", "а" * 6000)], limit=2500)
        self.assertEqual(sum(len(c["text"]) for c in chunks), 6000)
        self.assertTrue(all(len(c["text"]) <= 2500 for c in chunks))

    def test_split_parts(self):
        chunks = [{"lang": "uk", "text": "x" * 1000} for _ in range(10)]
        parts = T.split_parts(chunks, part_chars=3500)
        self.assertEqual([len(p) for p in parts], [3, 3, 3, 1])


FRAME = b"\xff\xf3\x64\xc0" + b"\x00" * 140            # MPEG-2 L3, 24 кГц, 48 кбіт/с, моно = 144 байти
XING = b"\xff\xf3\x64\xc0" + b"\x00" * 9 + b"Xing" + b"\x00" * 127   # службовий кадр тієї ж довжини (144 байти)
ID3V2 = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10
ID3V1 = b"TAG" + b"\x00" * 125


class Mp3Tags(unittest.TestCase):
    def test_frame_length(self):
        self.assertEqual(T._first_frame_len(FRAME), 144)
        self.assertEqual(T._first_frame_len(b"ID3\x03"), 0)

    def test_clean_audio_is_untouched(self):
        self.assertEqual(T.strip_mp3_tags(FRAME * 5), FRAME * 5)

    def test_strips_id3v2_id3v1_and_xing(self):
        self.assertEqual(T.strip_mp3_tags(ID3V2 + FRAME * 5 + ID3V1), FRAME * 5)
        self.assertEqual(T.strip_mp3_tags(XING + FRAME * 5), FRAME * 5)
        self.assertEqual(T.strip_mp3_tags(ID3V2 + XING + FRAME * 5 + ID3V1), FRAME * 5)

    def test_id3v2_footer_and_size(self):
        big = b"ID3\x04\x00\x10\x00\x00\x01\x00" + b"\x00" * 128 + b"\x00" * 10   # розмір 128 + футер 10
        self.assertEqual(T.strip_mp3_tags(big + FRAME * 2), FRAME * 2)


class Convert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.epub = sample_epub(os.path.join(self.dir, "book.epub"))
        self.log = os.path.join(self.dir, "calls.log")
        os.environ["STUB_LOG"] = self.log
        for k in ("STUB_FAIL", "STUB_FLAKY"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.pop("STUB_LOG", None)
        for k in ("STUB_FAIL", "STUB_FLAKY"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def used_voices(self):
        with open(self.log, encoding="utf8") as f:
            return {line.split("\t")[0] for line in f}

    def run_convert(self, **kw):
        with redirect_stdout(io.StringIO()):
            return T.convert(self.epub, os.path.join(self.dir, "out"), **kw)

    def test_zip_is_stored_with_manifest_and_mp3(self):
        z = zipfile.ZipFile(self.run_convert())
        self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()), "плеєр читає лише нестиснений zip")
        man = json.loads(z.read("manifest.json"))
        self.assertEqual((man["title"], man["author"]), ("Тестова книга", "Автор"))
        self.assertEqual([c["title"] for c in man["chapters"]], ["Розділ перший", "Глава вторая"])
        for c in man["chapters"]:
            data = z.read(c["file"])
            self.assertEqual(data[:2], b"\xff\xf3")
            self.assertGreater(c["duration"], 0)

    def test_chapter_files_are_clean_frames_only(self):
        os.environ["STUB_TAGS"] = "1"          # заглушка додає ID3v2/ID3v1 до кожного фрагмента
        try:
            z = zipfile.ZipFile(self.run_convert())
        finally:
            os.environ.pop("STUB_TAGS", None)
        for name in z.namelist():
            if name.endswith(".mp3"):
                data = z.read(name)
                self.assertEqual(data[:2], b"\xff\xf3", name)
                self.assertNotIn(b"ID3", data, name)
                self.assertNotIn(b"TAG\x00", data, name)
                self.assertEqual(len(data) % 144, 0, "лише цілі кадри")

    def test_durations_match_audio(self):
        z = zipfile.ZipFile(self.run_convert())
        man = json.loads(z.read("manifest.json"))
        for c in man["chapters"]:
            frames = len(z.read(c["file"])) // 144
            self.assertAlmostEqual(c["duration"], frames * 0.024, delta=0.5, msg=c["file"])

    def test_voices_follow_languages(self):
        self.run_convert()
        voices = self.used_voices()
        self.assertEqual(voices, {"uk-UA-PolinaNeural", "en-GB-SoniaNeural", "ru-RU-SvetlanaNeural"})

    def test_male_voices_and_stable_book_id(self):
        id_f = json.loads(zipfile.ZipFile(self.run_convert()).read("manifest.json"))["id"]
        open(self.log, "w").close()
        id_m = json.loads(zipfile.ZipFile(self.run_convert(gender="male")).read("manifest.json"))["id"]
        voices = self.used_voices()
        self.assertEqual(voices, {"uk-UA-OstapNeural", "en-US-GuyNeural", "ru-RU-DmitryNeural"})
        self.assertEqual(id_f, id_m, "прогрес у плеєрі має лишатися при повторній озвучці іншим голосом")

    def test_retries_after_flaky_errors(self):
        flaky = os.path.join(self.dir, "flaky")
        os.makedirs(flaky)
        os.environ["STUB_FLAKY"] = flaky
        self.assertTrue(os.path.exists(self.run_convert()))

    def test_narrow_voice_gets_its_own_concurrency_limit(self):
        import edge_tts
        ru = "Съёмки продолжались всю ночь, и никто не ушёл домой до самого утра. "
        chapters = [(f"c{i}.xhtml", page(f"Глава {i}", ru * 40)) for i in range(1, 7)]
        epub = make_epub(os.path.join(self.dir, "ru.epub"), chapters, title="Русская", author="А")
        edge_tts.MAX_ACTIVE.clear()
        with redirect_stdout(io.StringIO()):
            T.convert(epub, os.path.join(self.dir, "out2"), gender="male", concurrency=6)
        self.assertLessEqual(edge_tts.MAX_ACTIVE["ru-RU-DmitryNeural"], 2)
        edge_tts.MAX_ACTIVE.clear()
        with redirect_stdout(io.StringIO()):
            T.convert(epub, os.path.join(self.dir, "out3"), gender="female", concurrency=6)
        self.assertGreater(edge_tts.MAX_ACTIVE["ru-RU-SvetlanaNeural"], 2, "інші голоси працюють паралельно")

    def test_dead_voice_falls_back_once_and_book_is_complete(self):
        os.environ["STUB_DEAD_VOICE"] = "ru-RU-DmitryNeural"
        self.addCleanup(lambda: os.environ.pop("STUB_DEAD_VOICE", None))
        ru = "Съёмки продолжались всю ночь, и никто не ушёл домой до самого утра. "
        chapters = [(f"c{i}.xhtml", page(f"Глава {i}", ru * 60)) for i in range(1, 5)]
        epub = make_epub(os.path.join(self.dir, "ru.epub"), chapters, title="Русская", author="А")
        with redirect_stdout(io.StringIO()):
            z = zipfile.ZipFile(T.convert(epub, os.path.join(self.dir, "outd"), gender="male", concurrency=6))
        man = json.loads(z.read("manifest.json"))
        self.assertEqual(len(man["chapters"]), 4)
        with open(self.log, encoding="utf8") as f:
            calls = [line.split("\t")[0] for line in f]
        self.assertIn("ru-RU-SvetlanaNeural", calls, "решту книги читає запасний голос")
        self.assertLessEqual(calls.count("ru-RU-DmitryNeural"), 2 * 6, "мертвий голос пробуємо лише для перших фрагментів, не для всієї книги")
        self.assertIn("ru-RU-DmitryNeural", T.DEAD_VOICES)

    def test_voice_without_fallback_still_fails(self):
        os.environ["STUB_DEAD_VOICE"] = "uk-UA-PolinaNeural"
        self.addCleanup(lambda: os.environ.pop("STUB_DEAD_VOICE", None))
        with self.assertRaises(RuntimeError):
            self.run_convert()

    def test_symbol_only_paragraphs_are_not_voiced(self):
        book = {"chapters": [{"title": "Ілюстрація", "paras": ["* * *", "—", "…"]},
                             {"title": "Розділ", "paras": ["* * *", "Він прийшов додому дуже пізно ввечері."]}]}
        parts = T.plan_book(book, "uk")
        self.assertEqual(len(parts), 1)
        self.assertNotIn("* * *", parts[0]["chunks"][0]["text"])

    def test_tiny_fragment_without_audio_is_skipped_not_fatal(self):
        os.environ["STUB_NOAUDIO"] = "Розділ перший"          # короткий заголовок: менше 20 літер... додаємо під довжину нижче
        self.addCleanup(lambda: os.environ.pop("STUB_NOAUDIO", None))
        async def go(text):
            sem = asyncio.Semaphore(1)
            path = os.path.join(self.dir, "x.mp3")
            await T.synth_chunk(text, "uk-UA-PolinaNeural", path, "+0%", sem)
            return path
        T.RETRY_DELAY = 0
        path = asyncio.run(go("Розділ перший"))
        self.assertGreater(T.mp3_duration(path), 0.3)          # тиша замість звуку
        self.assertEqual(T.STATS.get("skipped"), 1)

    def test_long_fragment_without_audio_fails_with_snippet(self):
        os.environ["STUB_NOAUDIO"] = "Він прийшов"
        self.addCleanup(lambda: os.environ.pop("STUB_NOAUDIO", None))
        T.RETRY_DELAY = 0
        text = "Він прийшов додому дуже пізно ввечері, коли всі вже спали."
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(T.synth_chunk(text, "uk-UA-PolinaNeural", os.path.join(self.dir, "y.mp3"), "+0%", asyncio.Semaphore(1)))
        self.assertIn("Він прийшов додому", str(cm.exception))

    def test_total_failure_leaves_no_zip(self):
        os.environ["STUB_FAIL"] = "1"
        with self.assertRaises(RuntimeError):
            self.run_convert()
        out = os.path.join(self.dir, "out")
        self.assertEqual([f for f in os.listdir(out) if f.endswith(".zip") or f.endswith(".part")], [])

    def test_work_files_stay_out_of_output_folder(self):
        self.run_convert()
        out = os.path.join(self.dir, "out")
        names = os.listdir(out)
        self.assertEqual([n for n in names if n.endswith(".audiobook.zip")], ["book.audiobook.zip"])
        self.assertNotIn(".work", names)                       # фрагментів у iCloud немає
        self.assertEqual([n for n in names if n.endswith(".part")], [])
        self.assertEqual(os.listdir(WORK), [])                 # локальна тека прибрана після збірки

    def test_hung_connection_is_retried(self):
        import edge_tts
        real = edge_tts.Communicate
        calls = {"n": 0}

        class Hanging(real):
            async def save(self, path):
                calls["n"] += 1
                if calls["n"] == 1:
                    await asyncio.sleep(30)  # перша спроба зависає
                return await super().save(path)

        edge_tts.Communicate = Hanging
        old = T.SYNTH_TIMEOUT
        T.SYNTH_TIMEOUT = 0.3
        try:
            path = os.path.join(self.dir, "x.mp3")
            asyncio.run(T.synth_chunk("Привіт, це перевірка зв'язку, яка достатньо довга.", "uk-UA-PolinaNeural", path, "+0%", asyncio.Semaphore(1)))
        finally:
            edge_tts.Communicate = real
            T.SYNTH_TIMEOUT = old
        self.assertTrue(os.path.exists(path))
        self.assertGreaterEqual(calls["n"], 2)

    def test_book_without_text_is_rejected(self):
        empty = make_epub(os.path.join(self.dir, "drm.epub"), [("c1.xhtml", "<html><body><p>—</p></body></html>")])
        with self.assertRaises(RuntimeError) as cm:
            with redirect_stdout(io.StringIO()):
                T.convert(empty, os.path.join(self.dir, "out2"))
        self.assertIn("DRM", str(cm.exception))

    def test_dry_run_reports_languages(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            T.dry_run(self.epub)
        out = buf.getvalue()
        self.assertIn("Тестова книга", out)
        self.assertIn("uk:", out)
        self.assertIn("ru:", out)

    def test_command_line(self):
        r = run_tool(self.epub, "--out", os.path.join(self.dir, "cli"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "cli", "book.audiobook.zip")))


class EasyMode(unittest.TestCase):
    """Режим «подвійний клік / перетягнути файл» (для exe)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.epub = sample_epub(os.path.join(self.dir, "book.epub"))

    def run_easy(self, *args, stdin="1\n", timeout=300, declined=True):
        import subprocess
        sp = os.path.join(self.dir, "appdata", "Ozvuchka", "settings.json")
        if declined and not os.path.exists(sp):   # питання про сховище в цих тестах не цікаве
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            with open(sp, "w", encoding="utf-8") as fh:
                json.dump({"storage_declined": True}, fh)
        env = dict(os.environ, PYTHONPATH=os.path.join(HERE, "stub"), HOME=self.home,
                   USERPROFILE=self.home, APPDATA=os.path.join(self.dir, "appdata"))
        return subprocess.run([sys.executable, T.__file__, *args], input=stdin, capture_output=True,
                              text=True, env=env, timeout=timeout)

    def test_find_icloud_variants(self):
        self.assertIsNone(T.find_icloud(self.home))
        os.makedirs(os.path.join(self.home, "iCloud Drive"))
        self.assertTrue(str(T.find_icloud(self.home)).endswith("iCloud Drive"))

    def test_dropped_file_without_icloud_goes_next_to_book(self):
        r = self.run_easy(self.epub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "book.audiobook.zip")))

    def test_dropped_file_with_icloud_goes_to_ozvucheno(self):
        os.makedirs(os.path.join(self.home, "iCloudDrive"))
        r = self.run_easy(self.epub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, "iCloudDrive", "Озвучено", "book.audiobook.zip")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "iCloudDrive", "Книги")))

    def test_double_click_without_icloud_explains(self):
        r = self.run_easy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("iCloud", r.stdout)

    def test_icloud_path_can_be_typed_and_is_remembered(self):
        custom = os.path.join(self.dir, "Мій iCloud")
        os.makedirs(custom)
        import subprocess
        try:  # після створення папок програма стежить безкінечно: зупиняємо за таймаутом
            self.run_easy(stdin="1\n" + custom + "\n", timeout=6)
        except subprocess.TimeoutExpired:
            pass
        with open(os.path.join(self.dir, "appdata", "Ozvuchka", "settings.json"), encoding="utf-8") as fh:
            self.assertIn("Мій iCloud", fh.read())
        self.assertTrue(os.path.isdir(os.path.join(custom, "Книги")))

    def test_icloud_folder_found_by_prefix(self):
        os.makedirs(os.path.join(self.home, "iCloud Drive (Anton)"))
        self.assertIsNotNone(T.find_icloud(self.home))

    def test_progress_line_and_log_file(self):
        r = self.run_easy(self.epub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("остання відповідь", r.stdout)
        self.assertIn("Збираю книгу", r.stdout)
        with open(os.path.join(self.dir, "appdata", "Ozvuchka", "log.txt"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("запуск програми", log)
        self.assertIn("озвучку завершено", log)

    def test_voice_choice_is_remembered(self):
        self.run_easy(self.epub, stdin="2\n")
        with open(os.path.join(self.dir, "appdata", "Ozvuchka", "settings.json"), encoding="utf-8") as fh:
            saved = fh.read()
        self.assertIn("male", saved)
        r = self.run_easy(self.epub, stdin="")
        self.assertNotIn("Який голос", r.stdout)


if __name__ == "__main__":
    unittest.main()


class StorageUpload(unittest.TestCase):
    """Завантаження в S3-сумісне сховище: підписаний PUT, посилання з випадковою частиною."""

    def setUp(self):
        import http.server, threading
        got = self.got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def do_PUT(self):
                n = int(self.headers["Content-Length"])
                got["path"] = self.path
                got["headers"] = dict(self.headers)
                got["body"] = self.rfile.read(n)
                code = got.get("code", 200)
                self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.dir = tempfile.mkdtemp()
        self.f = os.path.join(self.dir, "book.audiobook.zip")
        with open(self.f, "wb") as fh:
            fh.write(os.urandom(300000))
        self.cfg = {"endpoint": "http://127.0.0.1:%d" % self.srv.server_address[1], "region": "auto", "bucket": "mybucket",
                    "key_id": "KEYID", "secret": "SECRETVALUE", "public_base": "https://files.example/mybucket"}

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()

    def test_upload_signs_and_returns_link(self):
        with redirect_stdout(io.StringIO()):
            link = T.upload_book(self.cfg, self.f)
        self.assertTrue(link.startswith("https://files.example/mybucket/"))
        self.assertTrue(link.endswith(".audiobook.zip"))
        self.assertTrue(self.got["path"].startswith("/mybucket/"))
        with open(self.f, "rb") as fh:
            self.assertEqual(self.got["body"], fh.read())
        auth = self.got["headers"]["authorization"]
        self.assertIn("AWS4-HMAC-SHA256 Credential=KEYID/", auth)
        self.assertNotIn("SECRETVALUE", json.dumps(self.got["headers"]))
        self.assertTrue(self.got["path"].endswith(link.rsplit("/", 1)[1]))

    def test_links_are_unique(self):
        with redirect_stdout(io.StringIO()):
            a = T.upload_book(self.cfg, self.f); b = T.upload_book(self.cfg, self.f)
        self.assertNotEqual(a, b)

    def test_server_error_is_reported(self):
        self.got["code"] = 403
        T.time_sleep = None
        orig = T.time.sleep; T.time.sleep = lambda s: None
        try:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    T.upload_book(self.cfg, self.f)
        finally:
            T.time.sleep = orig


class CoverTests(unittest.TestCase):
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa\x00\x00\x00\x00IEND\xaeB`\x82")

    def make(self, items, name="b.epub"):
        d = tempfile.mkdtemp()
        return make_epub(os.path.join(d, name), [("c1.xhtml", page("Розділ", UK * 3))], extra_items=items,
                         files={"images/cover.png": self.PNG})

    def test_cover_image_property(self):
        p = self.make(['<item id="img1" href="images/cover.png" media-type="image/png" properties="cover-image"/>'])
        c = T.read_epub(p)["cover"]
        self.assertEqual((c[0], c[1]), (self.PNG, "png"))

    def test_cover_by_id_name(self):
        p = self.make(['<item id="cover-img" href="images/cover.png" media-type="image/png"/>'])
        self.assertEqual(T.read_epub(p)["cover"][1], "png")

    def test_no_cover_is_fine(self):
        p = self.make([])
        self.assertIsNone(T.read_epub(p)["cover"])

    def test_cover_goes_into_zip_and_manifest(self):
        p = self.make(['<item id="img1" href="images/cover.png" media-type="image/png" properties="cover-image"/>'])
        out = tempfile.mkdtemp()
        r = run_tool(p, "--out", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        z = zipfile.ZipFile(os.path.join(out, "b.audiobook.zip"))
        man = json.loads(z.read("manifest.json"))
        self.assertEqual(man["cover"], "cover.png")
        self.assertEqual(z.read("cover.png"), self.PNG)


class NotifyTests(unittest.TestCase):
    """Сповіщення в Telegram через Supabase RPC і завантаження одним викликом publish()."""

    def setUp(self):
        import http.server, threading
        got = self.got = {"rpc": [], "put": 0}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def _body(self):
                n = int(self.headers.get("Content-Length", 0)); return self.rfile.read(n)
            def do_PUT(self):
                self._body(); got["put"] += 1
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
            def do_POST(self):
                b = self._body(); got["rpc"].append((self.path, dict(self.headers), json.loads(b)))
                code = got.get("code", 200)
                out = b'{"ok": true}' if code == 200 else b'{"message":"forbidden"}'
                self.send_response(code); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.dir = tempfile.mkdtemp()
        self.zip = os.path.join(self.dir, "b.audiobook.zip")
        with zipfile.ZipFile(self.zip, "w", zipfile.ZIP_STORED) as z:
            z.writestr("manifest.json", json.dumps({"id": "x", "title": "Чорти", "author": "Аберкромбі",
                                                    "chapters": [{"file": "001.mp3", "title": "a", "duration": 3600.5}, {"file": "002.mp3", "title": "b", "duration": 60}]}))
            z.writestr("001.mp3", b"x" * 1000)
        self.old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.dir
        T.save_settings(storage={"endpoint": self.base, "region": "auto", "bucket": "b", "key_id": "K", "secret": "S",
                                 "public_base": "https://files.example/b"},
                        notify={"url": self.base, "anon_key": "ANON", "upload_key": "UPKEY"})

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        if self.old_appdata is None: os.environ.pop("APPDATA", None)
        else: os.environ["APPDATA"] = self.old_appdata

    def test_publish_uploads_and_notifies(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            link = T.publish(self.zip)
        self.assertTrue(link.startswith("https://files.example/b/"))
        self.assertEqual(self.got["put"], 1)
        path, headers, body = self.got["rpc"][0]
        self.assertEqual(path, "/rest/v1/rpc/reader_book_ready")
        self.assertEqual({k.lower(): v for k, v in headers.items()}["apikey"], "ANON")
        self.assertEqual((body["k"], body["p_title"], body["p_author"], body["p_link"], body["p_dur"]),
                         ("UPKEY", "Чорти", "Аберкромбі", link, 3660))
        self.assertIn("Telegram надіслано", buf.getvalue())
        self.assertNotIn("UPKEY", buf.getvalue())

    def test_notify_failure_keeps_link(self):
        self.got["code"] = 403
        buf = io.StringIO()
        with redirect_stdout(buf):
            link = T.publish(self.zip)
        self.assertTrue(link)
        self.assertIn("не вдалося", buf.getvalue())
        self.assertNotIn("UPKEY", buf.getvalue())

    def test_without_notify_settings_nothing_is_sent(self):
        T.save_settings(notify={})
        with redirect_stdout(io.StringIO()):
            T.publish(self.zip)
        self.assertEqual(self.got["rpc"], [])

    def test_easy_jobs_setting(self):
        self.assertEqual(T.easy_jobs(), 6)
        T.save_settings(jobs=12); self.assertEqual(T.easy_jobs(), 12)
        T.save_settings(jobs=99); self.assertEqual(T.easy_jobs(), 20)
        T.save_settings(jobs="abc"); self.assertEqual(T.easy_jobs(), 6)


class RobustnessTests(unittest.TestCase):
    def test_replace_retries_when_file_is_locked(self):
        import unittest.mock as mock
        d = tempfile.mkdtemp(); src = os.path.join(d, "a.part"); dst = os.path.join(d, "a.mp3")
        open(src, "wb").write(b"x")
        real = os.replace; calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(32, "being used by another process")
            return real(a, b)
        with mock.patch.object(T.os, "replace", flaky):
            asyncio.run(T.replace_with_retry(src, dst, tries=5, delay=0))
        self.assertTrue(os.path.exists(dst)); self.assertEqual(calls["n"], 3)

    def test_replace_gives_up_eventually(self):
        import unittest.mock as mock
        d = tempfile.mkdtemp(); src = os.path.join(d, "a.part"); open(src, "wb").write(b"x")

        def locked(a, b): raise PermissionError(32, "locked")
        with mock.patch.object(T.os, "replace", locked):
            with self.assertRaises(PermissionError):
                asyncio.run(T.replace_with_retry(src, os.path.join(d, "a.mp3"), tries=3, delay=0))

    def test_second_copy_is_refused(self):
        import subprocess
        d = tempfile.mkdtemp()
        env = dict(os.environ, PYTHONPATH=os.path.join(HERE, "stub"), HOME=d, USERPROFILE=d, APPDATA=os.path.join(d, "appdata"))
        os.makedirs(os.path.join(d, "appdata", "Ozvuchka"))
        holder = subprocess.Popen([sys.executable, "-c",
            "import sys,time; sys.path.insert(0, %r); import audiobook_tool as T; h = T.single_instance_lock(); print('locked' if h else 'no', flush=True); time.sleep(20)" % os.path.join(ROOT, "tool")],
            stdout=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            r = subprocess.run([sys.executable, T.__file__], input="1\n", capture_output=True, text=True, env=env, timeout=30)
            self.assertIn("вже запущена", r.stdout)
        finally:
            holder.kill(); holder.wait()


class CoverVariants(unittest.TestCase):
    PNG = CoverTests.PNG
    JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 20000

    def build(self, items, files, guide=""):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "b.epub")
        make_epub(p, [("c1.xhtml", page("Розділ", UK * 3))], extra_items=items, files=files)
        if guide:   # додаємо <guide> до OPF
            import shutil
            tmp = p + ".tmp"
            with zipfile.ZipFile(p) as zin, zipfile.ZipFile(tmp, "w") as zout:
                for it in zin.infolist():
                    data = zin.read(it.filename)
                    if it.filename.endswith("content.opf"):
                        data = data.replace(b"</package>", guide.encode() + b"</package>")
                    zout.writestr(it, data)
            shutil.move(tmp, p)
        return p

    def test_wrong_media_type_jpg(self):   # image/jpg замість image/jpeg
        p = self.build(['<item id="cover" href="cover.jpg" media-type="image/jpg"/>'], {"cover.jpg": self.JPG})
        self.assertEqual(T.read_epub(p)["cover"][1], "jpg")

    def test_meta_cover_points_to_page_with_image(self):
        page_xml = '<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="images/pic.png" alt=""/></body></html>'
        p = self.build(['<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>'],
                       {"cover.xhtml": page_xml, "images/pic.png": self.PNG})
        # додаємо meta cover -> coverpage
        import shutil
        tmp = p + ".tmp"
        with zipfile.ZipFile(p) as zin, zipfile.ZipFile(tmp, "w") as zout:
            for it in zin.infolist():
                data = zin.read(it.filename)
                if it.filename.endswith("content.opf"):
                    data = data.replace(b"</metadata>", b'<meta name="cover" content="coverpage"/></metadata>')
                zout.writestr(it, data)
        shutil.move(tmp, p)
        self.assertEqual(T.read_epub(p)["cover"][1], "png")

    def test_guide_reference(self):
        p = self.build(['<item id="x1" href="pics/a.png" media-type="image/png"/>'], {"pics/a.png": self.PNG},
                       guide='<guide><reference type="cover" href="pics/a.png" title="Cover"/></guide>')
        self.assertEqual(T.read_epub(p)["cover"][1], "png")

    def test_url_encoded_href(self):
        p = self.build(['<item id="cover-image" href="my%20cover.png" media-type="image/png"/>'], {"my cover.png": self.PNG})
        self.assertEqual(T.read_epub(p)["cover"][1], "png")

    def test_format_detected_by_content_not_declared_type(self):
        p = self.build(['<item id="cover" href="cover.bin" media-type="image/png"/>'], {"cover.bin": self.JPG})
        self.assertEqual(T.read_epub(p)["cover"][1], "jpg")

    def test_non_image_cover_ignored(self):
        p = self.build(['<item id="cover" href="cover.png" media-type="image/png"/>'], {"cover.png": b"not an image at all"})
        self.assertIsNone(T.read_epub(p)["cover"])


class CloudJob(unittest.TestCase):
    """tool/cloud_job.py: хмарна озвучка зі змінними середовища (локальні сервери замість R2 і Supabase)."""

    def setUp(self):
        import http.server, threading
        sys.path.insert(0, os.path.join(ROOT, "tool"))
        import cloud_job
        self.cj = cloud_job
        prev = os.environ.pop("GITHUB_ACTIONS", None)      # тести не залежать від того, де вони запущені (у GitHub ця змінна є завжди)
        self.addCleanup(lambda: os.environ.__setitem__("GITHUB_ACTIONS", prev) if prev is not None else os.environ.pop("GITHUB_ACTIONS", None))
        got = self.got = {"put": 0, "rpc": [], "rpc_names": [], "delete": []}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def _b(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))
            def do_PUT(self):
                self._b(); got["put"] += 1
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
            def do_DELETE(self):
                got["delete"].append(self.path)
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
            def do_POST(self):
                got["rpc"].append(json.loads(self._b())); got["rpc_names"].append(self.path.rsplit("/", 1)[1])
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.env = {"R2_ENDPOINT": base, "R2_BUCKET": "b", "R2_KEY_ID": "K", "R2_SECRET": "S",
                    "R2_PUBLIC_BASE": "https://files.example/b", "SUPABASE_URL": base, "SUPABASE_ANON_KEY": "A",
                    "READER_UPLOAD_KEY": "U", "EPUB_URL": "https://files.example/in/book.epub", "JOBS": "3"}
        self.old = {k: os.environ.get(k) for k in list(self.env) + ["GENDER"]}
        os.environ.update(self.env)
        self.dir = tempfile.mkdtemp()
        self.epub = sample_epub(os.path.join(self.dir, "src.epub"))

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        for k, v in self.old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

    def test_end_to_end(self):
        import shutil, unittest.mock as mock
        with mock.patch.object(self.cj, "download", lambda url, dest: shutil.copy(self.epub, dest)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.cj.main()
        self.assertEqual(self.got["put"], 1)
        self.assertEqual(len(self.got["rpc"]), 1)
        self.assertEqual(self.got["rpc"][0]["k"], "U")
        self.assertTrue(self.got["rpc"][0]["p_link"].startswith("https://files.example/b/"))
        self.assertNotIn("SECRET", buf.getvalue())

    def _run(self, **extra):
        import shutil, unittest.mock as mock
        os.environ.update(extra)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in extra])
        with mock.patch.object(self.cj, "download", lambda url, dest: shutil.copy(self.epub, dest)):
            with redirect_stdout(io.StringIO()):
                self.cj.main()

    def test_job_status_flow_and_input_deleted(self):
        self._run(JOB_ID="42", EPUB_URL="https://files.example/b/in/tok/book.epub")
        self.assertEqual(self.got["rpc_names"], ["reader_job_update", "reader_job_title", "reader_book_ready", "reader_job_update"])
        self.assertEqual([r.get("p_status") for r in self.got["rpc"] if "p_status" in r], ["running", "done"])
        t = [r for r in self.got["rpc"] if "p_title" in r and "p_job" in r]
        self.assertEqual(len(t), 1)
        self.assertTrue(t[0]["p_title"] and t[0]["p_title"] != "Без назви" and t[0]["k"] == "U")
        self.assertTrue(all(r["p_job"] == 42 for r in self.got["rpc"] if "p_job" in r))
        self.assertEqual(self.got["delete"], ["/b/in/tok/book.epub"])

    def test_failure_is_reported_and_input_deleted(self):
        import unittest.mock as mock
        os.environ.update(JOB_ID="7", EPUB_URL="https://files.example/b/in/tok/book.epub")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("JOB_ID",)])
        def boom(url, dest): raise SystemExit("EPUB завеликий (понад 150 МБ)")
        with mock.patch.object(self.cj, "download", boom), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.cj.main()
        last = self.got["rpc"][-1]
        self.assertEqual((last["p_status"], last["p_job"]), ("failed", 7))
        self.assertIn("150", last["p_err"])
        self.assertEqual(self.got["delete"], ["/b/in/tok/book.epub"])

    def test_foreign_epub_url_is_not_deleted(self):
        self._run(JOB_ID="1", EPUB_URL="https://elsewhere.example/in/book.epub")
        self.assertEqual(self.got["delete"], [])
        self.assertIsNone(self.cj.input_key({"public_base": "https://files.example/b"}, "https://files.example/b/in/../x.epub"))
        self.assertIsNone(self.cj.input_key({"public_base": "https://files.example/b"}, "https://files.example/b/zip/x.audiobook.zip"))

    def test_delete_is_signed(self):
        seen = {}
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **k): pass
            def do_DELETE(self):
                seen.update(self.headers); seen["path"] = self.path
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cfg = {"endpoint": "http://127.0.0.1:%d" % srv.server_address[1], "region": "auto", "bucket": "b", "key_id": "KID", "secret": "SEC", "public_base": "x"}
            self.assertTrue(self.cj.s3_delete(cfg, "in/t/book.epub"))
        finally:
            srv.shutdown(); srv.server_close()
        self.assertEqual(seen["path"], "/b/in/t/book.epub")
        self.assertIn("Credential=KID/", seen["authorization"])
        self.assertNotIn("SEC", json.dumps(dict(seen)).replace("SignedHeaders", ""))

    def test_apple_two_stages(self):
        import shutil, unittest.mock as mock
        import apple_voice
        parts = os.path.join(self.dir, "apple-parts")
        os.environ.update({"GENDER": "apple", "APPLE_DIR": parts, "JOB_ID": "7", "EPUB_URL": "https://files.example/b/in/tok/book.epub"})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("GENDER", "APPLE_DIR", "JOB_ID", "STAGE")])
        real_synth, real_pack = apple_voice.synth, apple_voice.pack
        with mock.patch.object(self.cj, "download", lambda url, dest: shutil.copy(self.epub, dest)), \
             mock.patch.object(apple_voice, "synth", lambda e, d, jobs=3: real_synth(e, d, jobs, say=FAKE_SAY)), \
             mock.patch.object(apple_voice, "pack", lambda d, e, o: real_pack(d, e, o, ffmpeg=FAKE_FFMPEG)):
            os.environ["STAGE"] = "apple-synth"
            with redirect_stdout(io.StringIO()):
                self.cj.main()
            self.assertEqual(self.got["put"], 0, "на Маку книга ще не завантажується")
            self.assertEqual(self.got["delete"], [], "EPUB ще потрібен другому етапу")
            self.assertEqual(self.got["rpc_names"], ["reader_job_update", "reader_job_title"])
            self.assertEqual(self.got["rpc"][0]["p_status"], "running")
            self.assertTrue(os.path.exists(os.path.join(parts, "plan.json")))
            os.environ["STAGE"] = "apple-pack"
            with redirect_stdout(io.StringIO()):
                self.cj.main()
        self.assertEqual(self.got["put"], 1)
        self.assertEqual(self.got["rpc_names"][2:], ["reader_book_ready", "reader_job_update"])
        self.assertEqual(self.got["rpc"][-1]["p_status"], "done")
        self.assertEqual(len(self.got["delete"]), 1, "після збірки вхідний EPUB видалено")

    def test_apple_synth_failure_marks_failed_and_deletes_input(self):
        import shutil, unittest.mock as mock
        import apple_voice
        os.environ.update({"GENDER": "apple", "STAGE": "apple-synth", "APPLE_DIR": os.path.join(self.dir, "p"), "JOB_ID": "8",
                           "EPUB_URL": "https://files.example/b/in/tok/book.epub"})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("GENDER", "APPLE_DIR", "JOB_ID", "STAGE")])
        def boom(*a, **k):
            raise RuntimeError("голос Lesya не записав фрагмент")
        with mock.patch.object(self.cj, "download", lambda url, dest: shutil.copy(self.epub, dest)), \
             mock.patch.object(apple_voice, "synth", boom):
            with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
                self.cj.main()
        self.assertEqual(self.got["rpc"][-1]["p_status"], "failed")
        self.assertIn("Lesya", self.got["rpc"][-1]["p_err"])
        self.assertEqual(len(self.got["delete"]), 1)

    def test_public_log_hides_title_and_snippet_but_reader_gets_reason(self):
        import shutil, unittest.mock as mock
        import apple_voice
        os.environ.update({"GENDER": "apple", "STAGE": "apple-synth", "APPLE_DIR": os.path.join(self.dir, "p"), "JOB_ID": "8",
                           "EPUB_URL": "https://files.example/b/in/tok/book.epub", "GITHUB_ACTIONS": "true"})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("GENDER", "APPLE_DIR", "JOB_ID", "STAGE")])
        def boom(*a, **k):
            raise RuntimeError("голос Lesya не записав фрагмент «СЕКРЕТНИЙ УРИВОК КНИГИ»")
        out = io.StringIO()
        with mock.patch.object(self.cj, "download", lambda url, dest: shutil.copy(self.epub, dest)), \
             mock.patch.object(apple_voice, "synth", boom):
            with self.assertRaises(SystemExit) as cm, redirect_stdout(out):
                self.cj.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertNotIn("СЕКРЕТНИЙ", out.getvalue())
        self.assertIn("СЕКРЕТНИЙ УРИВОК", self.got["rpc"][-1]["p_err"], "у читалці причина з уривком лишається")

    def test_titles_hidden_in_public_log(self):
        self.assertEqual(T.pub("Назва книги"), "Назва книги")
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(T.pub("Назва книги"), "…")

    def test_apple_without_stage_is_refused(self):
        os.environ["GENDER"] = "apple"
        self.addCleanup(lambda: os.environ.pop("GENDER", None))
        with self.assertRaises(SystemExit):
            self.cj.main()
        self.assertEqual(self.got["rpc"], [])

    def test_missing_secrets(self):
        env = dict(self.env); env.pop("R2_SECRET")
        with self.assertRaises(SystemExit):
            self.cj.storage_from_env(env)

    def test_notify_optional(self):
        env = dict(self.env); env.pop("READER_UPLOAD_KEY")
        self.assertIsNone(self.cj.notify_from_env(env))

    def test_only_https_epub(self):
        with self.assertRaises(SystemExit):
            self.cj.download("http://files.example/a.epub", os.path.join(self.dir, "x.epub"))


STUB = os.path.join(HERE, "stub")
FAKE_SAY = os.path.join(STUB, "fake_say.py")
FAKE_FFMPEG = os.path.join(STUB, "fake_ffmpeg.py")


class AppleVoice(unittest.TestCase):
    """tool/apple_voice.py: запис голосами Apple (заглушка say) і збірка книги (заглушка ffmpeg)."""

    def setUp(self):
        import apple_voice
        self.A = apple_voice
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.epub = sample_epub(os.path.join(self.dir, "book.epub"))
        self.log = os.path.join(self.dir, "calls.log")
        os.environ["STUB_LOG"] = self.log

    def tearDown(self):
        for k in ("STUB_LOG", "FAKE_SAY_FAIL"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def calls(self):
        with open(self.log, encoding="utf8") as f:
            return [line.split("\t")[0] for line in f]

    def synth(self, epub=None):
        with redirect_stdout(io.StringIO()):
            return self.A.synth(epub or self.epub, os.path.join(self.dir, "parts"), jobs=3, say=FAKE_SAY)

    def test_segments_merge_same_language_and_respect_limit(self):
        part = {"chunks": [{"lang": "uk", "text": "а" * 100}, {"lang": "uk", "text": "б" * 100},
                           {"lang": "en", "text": "c" * 100}, {"lang": "uk", "text": "д" * 100}]}
        self.assertEqual([l for l, _ in self.A.segments(part)], ["uk", "en", "uk"])
        self.assertEqual(len(self.A.segments(part, limit=150)), 4)

    def test_voices_follow_languages_and_plan_is_written(self):
        plan = self.synth()
        self.assertEqual(set(self.calls()), {"Lesya", "Milena", "Samantha"})
        self.assertEqual([p["title"] for p in plan["parts"]], ["Розділ перший", "Глава вторая"])
        self.assertEqual((plan["title"], plan["author"]), ("Тестова книга", "Автор"))
        parts = os.path.join(self.dir, "parts")
        saved = json.load(open(os.path.join(parts, "plan.json"), encoding="utf8"))
        self.assertEqual(saved, plan)
        files = [f for p in plan["parts"] for f in p["files"]]
        self.assertTrue(all(os.path.exists(os.path.join(parts, f)) for f in files))
        self.assertFalse([f for f in os.listdir(parts) if f.endswith(".txt")], "тимчасові тексти прибрано")

    def test_failed_voice_stops_with_snippet(self):
        os.environ["FAKE_SAY_FAIL"] = "Milena"
        with self.assertRaises(RuntimeError) as cm:
            self.synth()
        self.assertIn("Milena", str(cm.exception))
        self.assertIn("Это второй раздел", str(cm.exception))

    def test_pack_builds_same_format_as_edge(self):
        self.synth()
        with redirect_stdout(io.StringIO()):
            zp = self.A.pack(os.path.join(self.dir, "parts"), self.epub, os.path.join(self.dir, "out"), ffmpeg=FAKE_FFMPEG)
        z = zipfile.ZipFile(zp)
        self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
        man = json.loads(z.read("manifest.json"))
        self.assertEqual([c["title"] for c in man["chapters"]], ["Розділ перший", "Глава вторая"])
        self.assertEqual(man["title"], "Тестова книга")
        for c in man["chapters"]:
            data = z.read(c["file"])
            self.assertEqual(data[:2], b"\xff\xf3")
            self.assertAlmostEqual(c["duration"], len(data) // 144 * 0.024, delta=0.5)
        tx = json.loads(z.read("text.json"))["chapters"]
        self.assertTrue(man["text"] and set(tx) == {c["file"] for c in man["chapters"]}, "текст для плеєра є в кожній частині")
        with redirect_stdout(io.StringIO()):
            edge = zipfile.ZipFile(T.convert(self.epub, os.path.join(self.dir, "edge")))
        self.assertEqual(json.loads(edge.read("manifest.json"))["id"], man["id"], "та сама книга має той самий id (прогрес у плеєрі)")

    def test_pack_refuses_when_parts_missing(self):
        plan = self.synth()
        os.remove(os.path.join(self.dir, "parts", plan["parts"][1]["files"][0]))
        with self.assertRaises(RuntimeError) as cm:
            with redirect_stdout(io.StringIO()):
                self.A.pack(os.path.join(self.dir, "parts"), self.epub, os.path.join(self.dir, "out"), ffmpeg=FAKE_FFMPEG)
        self.assertIn("бракує", str(cm.exception))
