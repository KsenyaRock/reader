#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Озвучка EPUB -> «аудіокнига» (zip з mp3 по розділах і manifest.json).

Використання:
  python audiobook_tool.py книга.epub            озвучити одну книгу
  python audiobook_tool.py --watch               стежити за папкою iCloud Drive\\Книги
  python audiobook_tool.py книга.epub --dry-run  лише показати розділи й мови, без озвучки

Потрібно: pip install edge-tts mutagen
"""
import argparse
import asyncio
import hashlib
import hmac
import http.client
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import json
import os
import posixpath
import random
import re
import shutil
import sys
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VOICES = {
    "female": {"uk": "uk-UA-PolinaNeural", "ru": "ru-RU-SvetlanaNeural", "en": "en-GB-SoniaNeural"},
    "male": {"uk": "uk-UA-OstapNeural", "ru": "ru-RU-DmitryNeural", "en": "en-US-GuyNeural"},
}
# Голоси, які Edge TTS не витримує паралельно (за пробою з GitHub: Dmitry при 8 паралельних запитах відповідає «No audio was received»
# на більшість, послідовно працює без збоїв): для них свій ліміт запитів
VOICE_LIMIT = {"ru-RU-DmitryNeural": 2}
# Запасні голоси: якщо голос перестав віддавати звук (Dmitry з жовтня 2026 відповідає «No audio was received» на все),
# після першого такого фрагмента всю решту книги читає запасний голос, а не падає вся озвучка
FALLBACK = {"ru-RU-DmitryNeural": "ru-RU-SvetlanaNeural"}
DEAD_VOICES = set()
CHUNK_CHARS = 2500       # розмір одного запиту до сервісу озвучки
RETRY_DELAY = 2         # секунд паузи між повторами запиту (множиться на номер спроби)
PART_CHARS = 45000       # довгі розділи ріжемо на частини (~1 година звуку)
MP3_BYTES_PER_SEC = 6000  # 48 кбіт/с, для оцінки тривалості, якщо немає mutagen


# ---------- читання EPUB ----------

class TextExtractor(HTMLParser):
    BLOCKS = {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "section"}
    SKIP = {"script", "style", "head", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras = []
        self.buf = []
        self.skip = 0
        self.heading = None
        self._in_heading = None
        self.etypes = set()

    def flush(self):
        text = clean(" ".join(self.buf))
        self.buf = []
        if text:
            self.paras.append(text)
            if self._in_heading and self.heading is None:
                self.heading = text

    def handle_starttag(self, tag, attrs):
        if tag in ("body", "section", "div", "nav", "aside"):
            for k, v in attrs:
                if k and v and k.split(":")[-1] in ("type", "role"):
                    self.etypes.update(v.lower().replace("doc-", "").split())
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCKS:
            self.flush()
            if tag in ("h1", "h2", "h3"):
                self._in_heading = tag

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.flush()
            if tag == self._in_heading:
                self._in_heading = None

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self.flush()


# ---------- пропуск службових розділів (обережно) ----------

JUNK_TYPES = {"toc", "copyright-page", "colophon", "imprint", "footnotes", "endnotes", "rearnotes", "index",
              "pagelist", "landmarks"}
JUNK_TITLE = re.compile(
    r"^\s*(зміст|содержание|оглавление|table of contents|contents|"
    r"про\s+видання|вихідні\s+дані|выходные\s+данные|copyright|авторські\s+права|colophon|колофон)\s*[.:]?\s*$", re.I)
RE_ISBN = re.compile(r"\bISBN\b|©", re.I)
JUNK_SHORT = 1500          # назви-пастки діють лише для розділів коротших за стільки символів
SKIP_LIMIT = 0.25          # якщо пропускається понад чверть тексту — не пропускаємо нічого


def junk_reason(title, paras, etypes):
    """Чому розділ службовий (або None). Правила свідомо обережні: краще озвучити зайве, ніж пропустити текст книги."""
    total = sum(len(p) for p in paras)
    hit = etypes & JUNK_TYPES
    if hit and total < 20000:
        return "позначено в книзі як " + sorted(hit)[0]
    if title and JUNK_TITLE.match(title) and total < JUNK_SHORT:
        return f"назва «{title}»"
    if total < 600 and any(RE_ISBN.search(p) for p in paras):
        return "вихідні дані (ISBN/©)"
    return None


def pub(text):
    """У журналі GitHub Actions (він публічний, якщо репозиторій відкритий) назви й уривки книги не показуємо."""
    return "…" if os.environ.get("GITHUB_ACTIONS") == "true" else text


def clean(text):
    text = text.replace("\u00ad", "").replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def localname(tag):
    return tag.rsplit("}", 1)[-1]


def _zread(z, base, href):
    """Читає файл з EPUB за відносним шляхом (з урахуванням %-кодування); повертає (шлях, байти) або None."""
    from urllib.parse import unquote
    href = href.split("#")[0].split("?")[0]
    for h in (href, unquote(href)):
        full = posixpath.normpath(posixpath.join(base, h)) if base else posixpath.normpath(h)
        try:
            return full, z.read(full)
        except KeyError:
            continue
    return None


def _sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _first_image_in_page(z, base, href):
    """Для обкладинки у вигляді сторінки xhtml: перше зображення на ній."""
    got = _zread(z, base, href)
    if not got:
        return None
    full, raw = got
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"""<(?:img|image)\b[^>]*?(?:src|xlink:href|href)\s*=\s*["']([^"']+)["']""", text, re.I | re.S)
    if not m:
        return None
    return _zread(z, posixpath.dirname(full), m.group(1))


def find_cover(z, base, manifest, cover_id, guide_href, spine):
    """Шукає обкладинку різними способами (EPUB3 cover-image, meta cover, guide, назва, перша сторінка).
    Повертає (байти, розширення) або None. Розмір до 6 МБ, формат визначається за вмістом файлу."""
    def take(got):
        if not got:
            return None
        data = got[1]
        ext = _sniff_image(data)
        return (data, ext) if ext and 0 < len(data) <= 6_000_000 else None

    def from_item(item):
        if not item:
            return None
        if item["type"].startswith("image/") or item["href"].lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            return take(_zread(z, base, item["href"]))
        if "html" in item["type"]:
            return take(_first_image_in_page(z, base, item["href"]))
        return None

    try:
        for item in manifest.values():                       # 1. EPUB3: properties="cover-image"
            if "cover-image" in item["props"].split():
                c = from_item(item)
                if c: return c
        c = from_item(manifest.get(cover_id)) if cover_id else None      # 2. <meta name="cover" content="id">
        if c: return c
        if guide_href:                                       # 3. <guide><reference type="cover">
            c = take(_zread(z, base, guide_href)) or take(_first_image_in_page(z, base, guide_href))
            if c: return c
        for k, item in manifest.items():                     # 4. зображення з "cover" у назві
            if "cover" in ((k or "") + item["href"]).lower() and item["type"].startswith("image/"):
                c = from_item(item)
                if c: return c
        for idref in spine[:3]:                              # 5. перше зображення на перших сторінках книги
            item = manifest.get(idref)
            if item and "html" in item["type"]:
                c = take(_first_image_in_page(z, base, item["href"]))
                if c and len(c[0]) > 15_000: return c
    except Exception:
        return None
    return None


def read_epub(path):
    """Повертає dict: title, author, lang, chapters=[{title, paras}]"""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = None
        for el in container.iter():
            if localname(el.tag) == "rootfile":
                opf_path = el.attrib["full-path"]
                break
        if not opf_path:
            raise ValueError("Не знайдено OPF у EPUB")
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)

        title = author = lang = ""
        cover_id = None
        guide_cover = None
        manifest = {}
        spine = []
        for el in opf.iter():
            name = localname(el.tag)
            if name == "title" and not title:
                title = clean(el.text or "")
            elif name == "creator" and not author:
                author = clean(el.text or "")
            elif name == "language" and not lang:
                lang = clean(el.text or "").lower()
            elif name == "reference" and el.attrib.get("type") == "cover" and not guide_cover:
                guide_cover = el.attrib.get("href", "")
            elif name == "meta" and el.attrib.get("name") == "cover":
                cover_id = el.attrib.get("content")
            elif name == "item":
                manifest[el.attrib.get("id")] = {
                    "href": el.attrib.get("href", ""),
                    "type": el.attrib.get("media-type", ""),
                    "props": el.attrib.get("properties", ""),
                }
            elif name == "itemref":
                spine.append(el.attrib.get("idref"))

        cover = find_cover(z, base, manifest, cover_id, guide_cover, spine)

        chapters = []
        for idref in spine:
            item = manifest.get(idref)
            if not item or "html" not in item["type"] or "nav" in item["props"].split():
                continue
            href = item["href"].split("#")[0]
            full = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
            try:
                raw = z.read(full)
            except KeyError:
                try:
                    from urllib.parse import unquote
                    raw = z.read(unquote(full))
                except KeyError:
                    continue
            parser = TextExtractor()
            parser.feed(raw.decode("utf-8", errors="replace"))
            parser.close()
            paras = [p for p in parser.paras if re.search(r"\w", p)]
            if sum(len(p) for p in paras) < 40:
                continue
            ch = {"title": parser.heading or "", "paras": paras}
            why = junk_reason(ch["title"], paras, parser.etypes)
            if why:
                ch["junk"] = why
            chapters.append(ch)

    for i, ch in enumerate(chapters, 1):
        if not ch["title"]:
            ch["title"] = f"Розділ {i}"
    total = sum(sum(len(p) for p in ch["paras"]) for ch in chapters)
    junk = [ch for ch in chapters if ch.get("junk")]
    junk_chars = sum(sum(len(p) for p in ch["paras"]) for ch in junk)
    if junk and junk_chars > SKIP_LIMIT * total:
        print(f"УВАГА: службовими визнано {junk_chars * 100 // max(total, 1)}% тексту — нічого не пропускаю", flush=True)
        junk = []
    if junk and len(junk) == len(chapters):
        junk = []
    for ch in junk:
        print(f"ПРОПУЩЕНО розділ «{pub(ch['title'][:60])}»: {ch['junk'] if not ch['junk'].startswith('назва') else 'за назвою'}", flush=True)
    skipped = [{"title": ch["title"][:80], "why": ch["junk"]} for ch in junk]
    chapters = [ch for ch in chapters if not any(ch is j for j in junk)]
    return {"title": title or "Без назви", "author": author, "lang": lang, "chapters": chapters, "cover": cover, "skipped": skipped}


# ---------- мова абзацу ----------

RE_CYR = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
RE_LAT = re.compile(r"[A-Za-z]")
RE_UK = re.compile(r"[іїєґІЇЄҐ]")
RE_RU = re.compile(r"[ыэъёЫЭЪЁ]")


def detect_lang(text):
    """uk / ru / en, 'cyr' (кирилиця без ознак мови), None (немає літер)"""
    cyr = len(RE_CYR.findall(text))
    lat = len(RE_LAT.findall(text))
    if cyr == 0 and lat == 0:
        return None
    if cyr >= lat:
        uk = len(RE_UK.findall(text))
        ru = len(RE_RU.findall(text))
        if uk > ru:
            return "uk"
        if ru > uk:
            return "ru"
        return "cyr"
    return "en"


def label_paragraphs(paras, default_cyr):
    langs = [detect_lang(p) for p in paras]
    # неоднозначні абзаци (короткі заголовки тощо) беруть мову найближчого визначеного кириличного сусіда:
    # спочатку наступного, інакше попереднього, інакше мови з метаданих
    resolved = []
    for i, lang in enumerate(langs):
        if lang in ("cyr", None):
            nxt = next((l for l in langs[i + 1:] if l in ("uk", "ru")), None)
            prev = next((l for l in reversed(resolved) if l in ("uk", "ru")), None)
            lang = nxt or prev or default_cyr
        resolved.append(lang)
    out = []
    for lang, p in zip(resolved, paras):
        if p[-1] not in ".!?…:;»\"”)":
            p += "."
        out.append((lang, p))
    return out


def split_long(text, limit):
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    pieces, cur = [], ""
    for s in sentences:
        while len(s) > limit:
            pieces.append(s[:limit])
            s = s[limit:]
        if cur and len(cur) + len(s) + 1 > limit:
            pieces.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        pieces.append(cur)
    return pieces


def make_chunks(labeled, limit=CHUNK_CHARS):
    chunks = []
    cur = None
    for lang, text in labeled:
        for piece in split_long(text, limit):
            if cur and cur["lang"] == lang and len(cur["text"]) + len(piece) + 2 <= limit:
                cur["text"] += "\n\n" + piece
            else:
                cur = {"lang": lang, "text": piece}
                chunks.append(cur)
    return chunks


def split_parts(chunks, part_chars=PART_CHARS):
    parts, cur, size = [], [], 0
    for c in chunks:
        if cur and size + len(c["text"]) > part_chars:
            parts.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c["text"])
    if cur:
        parts.append(cur)
    return parts



# ---------- чистка mp3 перед склеюванням ----------

_BR_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_BR_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _first_frame_len(data):
    """Довжина першого кадру MPEG Layer III або 0, якщо data не починається з кадру."""
    if len(data) < 4 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return 0
    ver = (data[1] >> 3) & 3          # 3 = MPEG1, 2 = MPEG2, 0 = MPEG2.5
    layer = (data[1] >> 1) & 3        # 1 = Layer III
    br_i, sr_i = data[2] >> 4, (data[2] >> 2) & 3
    if ver == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:
        return 0
    br = (_BR_V1 if ver == 3 else _BR_V2)[br_i] * 1000
    sr = _SR[ver][sr_i]
    pad = (data[2] >> 1) & 1
    return (144 if ver == 3 else 72) * br // sr + pad


def strip_mp3_tags(data):
    """Прибирає ID3v2 на початку, ID3v1 у кінці та службовий кадр Xing/Info.
    Це потрібно, щоб при склеюванні фрагментів у середині звуку не лишалось службових даних,
    на яких плеєр (особливо Safari на iPhone) може збитися або неправильно порахувати тривалість."""
    while data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        data = data[10 + size + (10 if data[5] & 0x10 else 0):]
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
    n = _first_frame_len(data)
    if n and (b"Xing" in data[:60] or b"Info" in data[:60]):
        data = data[n:]
    return data

# ---------- журнал ----------

STATS = {"active": 0, "retries": 0, "last_ok": None}
LOG_FILE = None  # шлях до файлу журналу (задається в режимі exe)


def log_event(text):
    """Дописує подію в файл журналу (якщо він увімкнений); помилки запису ігноруються."""
    if not LOG_FILE:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def fmt_eta(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600} год {sec % 3600 // 60} хв"
    return f"{sec // 60} хв" if sec >= 60 else f"{sec} с"


def disable_quick_edit():
    """Windows: клік у чорному вікні ставить програму на паузу. Вимикаємо цей режим."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, (mode.value & ~0x40) | 0x80)
    except Exception:
        pass


# ---------- озвучка ----------

APP_VERSION = "0.8.4"
SYNTH_TIMEOUT = 90  # с на одну спробу: якщо з'єднання зависло, перезапускаємо фрагмент
EASY_JOBS = 6  # паралельних запитів у режимі exe за замовчуванням; змінюється в settings.json: "jobs": 10


def easy_jobs():
    try:
        return max(1, min(20, int(load_settings().get("jobs", EASY_JOBS))))
    except (TypeError, ValueError):
        return EASY_JOBS


async def replace_with_retry(src, dst, tries=15, delay=0.5):
    """Windows: антивірус чи індексатор можуть кілька секунд тримати щойно створений файл (WinError 32): чекаємо й пробуємо знову."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            await asyncio.sleep(delay)


# півсекунди тиші (24 кГц, 48 кбіт/с, моно): 20 порожніх кадрів MPEG-2 Layer III
SILENT_MP3 = (b"\xff\xf3\x64\xc0" + b"\x00" * 140) * 20


async def synth_chunk(text, voice, path, rate, sem):
    import edge_tts
    last = None
    async with sem:
        for attempt in range(1, 7):
            STATS["active"] += 1
            try:
                tmp = Path(f"{path}.{attempt}.part")   # окремий файл на кожну спробу: недороблена спроба не блокує наступну
                await asyncio.wait_for(edge_tts.Communicate(text, voice, rate=rate).save(str(tmp)), SYNTH_TIMEOUT)
                if tmp.stat().st_size < 500:
                    raise RuntimeError("порожній звук")
                await replace_with_retry(tmp, path)
                STATS["last_ok"] = time.time()
                for old in Path(path).parent.glob(Path(path).name + ".*.part"):   # прибираємо залишки невдалих спроб
                    try: old.unlink()
                    except OSError: pass
                return
            except Exception as e:  # мережа, ліміти сервісу
                last = e
                STATS["retries"] += 1
                log_event(f"повтор {attempt}/6 для {Path(path).name}: {type(e).__name__} {e}")
                await asyncio.sleep(RETRY_DELAY * attempt * (1 + random.random()))
            finally:
                STATS["active"] -= 1
    letters = len(re.findall(r"[^\W\d_]", text))
    if "No audio was received" in str(last) and letters < 20:
        # крихітний фрагмент без мовного змісту (символи, номери): краще пропустити, ніж зупинити всю книгу
        Path(path).write_bytes(SILENT_MP3)
        STATS["skipped"] = STATS.get("skipped", 0) + 1
        log_event(f"ПРОПУЩЕНО {Path(path).name}: «{text[:40]}» (немає звуку)")
        return
    snippet = text[:60].replace("\n", " ")
    log_event(f"ПОМИЛКА {Path(path).name}: {last}; текст: «{snippet}»")
    raise RuntimeError(f"не вдалося озвучити фрагмент «{snippet}»: {last}")


def mp3_duration(path):
    try:
        from mutagen.mp3 import MP3
        return float(MP3(str(path)).info.length)
    except Exception:
        return Path(path).stat().st_size / MP3_BYTES_PER_SEC


def fmt_dur(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "book"


def plan_book(book, default_cyr):
    """Список частин: [{title, chunks:[{lang,text}]}]"""
    parts = []
    for ch in book["chapters"]:
        paras = [p for p in ch["paras"] if re.search(r"\w", p)]   # «* * *», «—», «…» без літер і цифр озвучити неможливо
        if not paras:
            continue
        labeled = label_paragraphs(paras, default_cyr)
        chunks = make_chunks(labeled)
        sub = split_parts(chunks)
        for i, sp in enumerate(sub, 1):
            title = ch["title"] if len(sub) == 1 else f"{ch['title']} (частина {i})"
            parts.append({"title": title, "chunks": sp})
    return parts


def default_cyr_for(book):
    return "ru" if book["lang"].startswith("ru") else "uk"


def dry_run(epub):
    book = read_epub(epub)
    if book.get("cover"):
        print(f"Обкладинка: знайдено ({len(book['cover'][0]) // 1024} КБ)")
    else:
        print("Обкладинка: у книзі не знайдено (її можна додати в читалці: книга → три крапки → «Обкладинка з фото»)")
    parts = plan_book(book, default_cyr_for(book))
    total = 0
    print(f"Книга: {book['title']} — {book['author']} (мова в метаданих: {book['lang'] or '?'})")
    for i, p in enumerate(parts, 1):
        chars = sum(len(c["text"]) for c in p["chunks"])
        langs = {}
        for c in p["chunks"]:
            langs[c["lang"]] = langs.get(c["lang"], 0) + len(c["text"])
        total += chars
        mix = ", ".join(f"{k}:{v * 100 // chars}%" for k, v in sorted(langs.items()))
        print(f"{i:3d}. {p['title'][:50]:50s} {chars:7d} символів  [{mix}]")
    print(f"Разом ~{total} символів, орієнтовно {fmt_dur(total / 15)} звуку")


async def run_synthesis(parts, workdir, gender, rate, concurrency):
    sem = asyncio.Semaphore(concurrency)
    jobs = []
    for pi, p in enumerate(parts):
        for ci, c in enumerate(p["chunks"]):
            path = workdir / f"{pi:04d}_{ci:04d}.mp3"
            c["path"] = path
            if not path.exists():
                jobs.append((c, path))
    total_chars = sum(len(c["text"]) for c, _ in jobs) or 1
    done = {"chars": 0, "n": 0}
    started = time.time()
    STATS.update(active=0, retries=0, last_ok=None)
    log_event(f"початок озвучки: {len(jobs)} фрагментів, {concurrency} потоків")

    def show_progress():
        el = time.time() - started
        pct = done["chars"] * 100 // total_chars
        eta = ""
        if done["chars"] and el > 20:
            eta = f" · лишилось ~{fmt_eta(el * (total_chars - done['chars']) / done['chars'])}"
        ago = time.time() - (STATS["last_ok"] or started)
        warn = "  ⚠ довго немає відповіді, чекаю..." if ago > 45 else ""
        line = (f"  озвучено {pct}% ({done['n']}/{len(jobs)}) · минуло {fmt_eta(el)}{eta}"
                f" · запитів {STATS['active']} · остання відповідь {int(ago)} с тому"
                f" · повторів {STATS['retries']}{warn}")
        if sys.stdout.isatty():
            print("\r" + line.ljust(118), end="", flush=True)
        else:
            print(line.strip(), flush=True)   # у журналі (GitHub Actions) окремі рядки, а не безкінечний \r

    async def ticker():
        while True:
            show_progress()
            await asyncio.sleep(1 if sys.stdout.isatty() else 60)

    voices = set(VOICES[gender].values())
    voices |= {FALLBACK[v] for v in voices if v in FALLBACK}
    vsems = {v: asyncio.Semaphore(min(concurrency, VOICE_LIMIT.get(v, concurrency))) for v in voices}
    DEAD_VOICES.clear()

    async def one(c, path):
        voice = VOICES[gender][c["lang"]]
        while True:
            if voice in DEAD_VOICES:
                voice = FALLBACK[voice]
            async with vsems[voice]:   # спершу ліміт голосу, щоб черга до «вузького» голосу не займала загальні місця
                if voice in DEAD_VOICES:
                    continue           # голос «замовк», поки фрагмент чекав у черзі: одразу до запасного
                try:
                    await synth_chunk(c["text"], voice, path, rate, sem)
                    break
                except RuntimeError as e:
                    if voice not in FALLBACK or "No audio was received" not in str(e):
                        raise
                    if voice not in DEAD_VOICES:
                        DEAD_VOICES.add(voice)
                        log_event(f"голос {voice} не віддає звук, далі читає {FALLBACK[voice]}")
                        print(f"\n  Голос {voice} не працює, решту читає {FALLBACK[voice]}", flush=True)
        done["chars"] += len(c["text"])
        done["n"] += 1
        if done["n"] % 25 == 0:
            log_event(f"готово {done['n']}/{len(jobs)} фрагментів")

    if jobs:
        tick = asyncio.create_task(ticker())
        try:
            await asyncio.gather(*(one(c, p) for c, p in jobs))
        finally:
            tick.cancel()
            show_progress()
        if sys.stdout.isatty():
            print()
        log_event("озвучку завершено")


def work_root():
    """Робоча тека для фрагментів: ЛОКАЛЬНА (не в iCloud), щоб тисячі файлів не синхронізувались."""
    env = os.environ.get("OZVUCHKA_WORK")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".ozvuchka")
    return Path(base) / "Ozvuchka" / "work"


def convert(epub_path, out_dir, gender="female", rate="+0%", concurrency=3):
    epub_path = Path(epub_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Читаю {epub_path.name} ...")
    book = read_epub(epub_path)
    if not book["chapters"]:
        raise RuntimeError("У книзі не знайдено тексту (можливо, файл захищений DRM)")
    parts = plan_book(book, default_cyr_for(book))
    chars = sum(len(c["text"]) for p in parts for c in p["chunks"])
    print(f"«{pub(book['title'])}»: {len(parts)} частин, {chars} символів, ~{fmt_dur(chars / 15)} звуку")

    # id книги не залежить від голосу: прогрес у плеєрі лишається, якщо книгу озвучили заново
    book_id = hashlib.sha1(f"{epub_path.stem}{epub_path.stat().st_size}".encode()).hexdigest()[:10]
    # робоча тека залежить від голосу й швидкості, щоб не змішати фрагменти з різних озвучок
    work_key = hashlib.sha1(f"{book_id}{gender}{rate}".encode()).hexdigest()[:10]
    workdir = work_root() / work_key
    workdir.mkdir(parents=True, exist_ok=True)

    log_event(f"книга «{book['title']}»: {len(parts)} частин, {chars} символів")
    asyncio.run(run_synthesis(parts, workdir, gender, rate, concurrency))
    print("Збираю книгу в один файл (може тривати хвилину)...")
    return assemble(book, parts, epub_path, out_dir, workdir, book_id)


def speech_weight(text):
    """Приблизний «час читання» абзацу в умовних символах: літери + паузи після розділових знаків і в кінці абзацу."""
    stops = len(re.findall(r"[.!?…]+", text))
    soft = len(re.findall(r"[,;:—–()«»]", text))
    return len(text) + 8 * stops + 3 * soft + 10


def text_timeline(chunks):
    """Текст для читання разом зі звуком: [[секунда від початку частини, абзац]]. Час у межах фрагмента
    розподіляється за довжиною абзаців (звук фрагмента відомий точно, тож похибка не накопичується)."""
    out, t = [], 0.0
    for c in chunks:
        dur = mp3_duration(c["path"])
        paras = [x for x in (c.get("text") or "").split("\n\n") if x.strip()]
        total = sum(speech_weight(x) for x in paras) or 1
        at = t
        for x in paras:
            out.append([round(at, 1), x])
            at += dur * speech_weight(x) / total
        t += dur
    return out


def assemble(book, parts, epub_path, out_dir, workdir, book_id):
    chapters_meta = []
    texts = {}
    tmp_dir = workdir / "chapters"
    tmp_dir.mkdir(exist_ok=True)
    for i, p in enumerate(parts, 1):
        fname = f"{i:03d}.mp3"
        fpath = tmp_dir / fname
        with open(fpath, "wb") as out:
            for c in p["chunks"]:
                out.write(strip_mp3_tags(Path(c["path"]).read_bytes()))
        chapters_meta.append({"file": fname, "title": p["title"], "duration": round(mp3_duration(fpath), 1)})
        if any(c.get("text") for c in p["chunks"]):
            texts[fname] = text_timeline(p["chunks"])

    manifest = {
        "id": book_id,
        "title": book["title"],
        "author": book["author"],
        "created": time.strftime("%Y-%m-%d"),
        "chapters": chapters_meta,
    }
    if texts:
        manifest["text"] = True
    if book.get("skipped"):
        manifest["skipped"] = book["skipped"]
    if book.get("cover"):
        manifest["cover"] = "cover." + book["cover"][1]
    zip_path = out_dir / f"{epub_path.stem}.audiobook.zip"
    local_zip = workdir / "book.audiobook.zip"  # збираємо локально, в iCloud кладемо готовий файл
    with zipfile.ZipFile(local_zip, "w", zipfile.ZIP_STORED) as z:  # без стиснення: плеєр читає частини напряму
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        if book.get("cover"):
            z.writestr(manifest["cover"], book["cover"][0])
        if texts:
            z.writestr("text.json", json.dumps({"v": 1, "chapters": texts}, ensure_ascii=False, separators=(",", ":")))
        for c in chapters_meta:
            z.write(tmp_dir / c["file"], c["file"])
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Копіюю готовий файл у папку «Озвучено»...")
    shutil.copyfile(local_zip, zip_path)
    shutil.rmtree(workdir, ignore_errors=True)
    total = sum(c["duration"] for c in chapters_meta)
    print(f"Готово: {zip_path}  ({fmt_dur(total)}, {zip_path.stat().st_size // 1048576} МБ)")
    return zip_path


# ---------- завантаження в сховище з прямим посиланням (S3-сумісне: Cloudflare R2, Backblaze B2) ----------

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def s3_put(cfg, key, path, content_type="application/zip", progress=None):
    """Одним запитом PUT кладе файл у бакет (підпис AWS SigV4). Секретні ключі беруться лише з локальних налаштувань."""
    path = Path(path)
    size = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    payload_hash = h.hexdigest()
    u = urllib.parse.urlparse(cfg["endpoint"])
    host = u.netloc
    region = cfg.get("region") or "auto"
    uri = "/" + urllib.parse.quote(f"{cfg['bucket']}/{key}")
    now = time.gmtime()
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", now)
    day = time.strftime("%Y%m%d", now)
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
               "content-type": content_type, "content-length": str(size)}
    names = sorted(headers)
    canonical = "PUT\n" + uri + "\n\n" + "".join(f"{n}:{headers[n]}\n" for n in names) + "\n" + ";".join(names) + "\n" + payload_hash
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
    k = _sign(("AWS4" + cfg["secret"]).encode(), day)
    for part in (region, "s3", "aws4_request"):
        k = _sign(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={cfg['key_id']}/{scope}, SignedHeaders={';'.join(names)}, Signature={sig}"
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(host, timeout=120)
    try:
        conn.putrequest("PUT", uri, skip_host=True, skip_accept_encoding=True)
        for n, v in headers.items():
            conn.putheader(n, v)
        conn.endheaders()
        sent = 0
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                conn.send(block)
                sent += len(block)
                if progress:
                    progress(sent, size)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"сховище відповіло {resp.status}: {body[:200].decode('utf-8', 'replace')}")
    finally:
        conn.close()


def upload_book(cfg, zip_path):
    """Завантажує готову книгу, повертає пряме посилання (з випадковою частиною, щоб його не вгадали)."""
    key = f"{secrets.token_urlsafe(9)}/{hashlib.sha1(Path(zip_path).name.encode()).hexdigest()[:10]}.audiobook.zip"
    last = [0.0]

    def prog(sent, size):
        if time.time() - last[0] > 1 or sent == size:
            last[0] = time.time()
            print(f"\r  Завантажую в сховище: {sent * 100 // size}% ({sent // 1048576} з {size // 1048576} МБ)   ", end="", flush=True)
    err = None
    for attempt in range(1, 4):
        try:
            s3_put(cfg, key, zip_path, progress=prog)
            print()
            link = cfg["public_base"].rstrip("/") + "/" + key
            log_event(f"завантажено в сховище: {Path(zip_path).name}")
            return link
        except Exception as e:
            err = e
            print(f"\n  Спроба {attempt} не вдалася: {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"не вдалося завантажити: {err}")


def copy_to_clipboard(text):
    try:
        subprocess.run(["clip"], input=text.encode("utf-16le"), check=True, timeout=10)
        return True
    except Exception:
        return False


STORAGE_FIELDS = [("endpoint", "Адреса сховища (S3 endpoint, https://...)"), ("region", "Регіон (для Cloudflare R2 напишіть auto)"),
                  ("bucket", "Назва бакета"), ("key_id", "Access Key ID"), ("secret", "Secret Access Key"),
                  ("public_base", "Публічна адреса бакета для посилань (https://...)")]


def storage_config():
    cfg = load_settings().get("storage") or {}
    return cfg if all(cfg.get(k) for k in ("endpoint", "bucket", "key_id", "secret", "public_base")) else None


def ask_storage():
    print("\nНалаштування завантаження за посиланням. Дані зберігаються лише на цьому комп'ютері.")
    cfg = {}
    for k, label in STORAGE_FIELDS:
        v = input(f"{label}: ").strip()
        if k == "region" and not v:
            v = "auto"
        cfg[k] = v
    save_settings(storage=cfg)
    return storage_config()


def notify_config():
    cfg = load_settings().get("notify") or {}
    return cfg if all(cfg.get(k) for k in ("url", "anon_key", "upload_key")) else None


def notify_ready(cfg, title, author, link, duration, size_mb):
    """Повідомляє Supabase, що книга готова (звідти йде повідомлення в Telegram). Ключі лише з локальних налаштувань."""
    body = json.dumps({"k": cfg["upload_key"], "p_title": title, "p_author": author, "p_link": link,
                       "p_dur": int(duration), "p_mb": int(size_mb)}).encode("utf-8")
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/rest/v1/rpc/reader_book_ready", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "apikey": cfg["anon_key"],
                                          "Authorization": "Bearer " + cfg["anon_key"]})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase відповів {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")


def publish(zip_path, cfg=None, nc=None, clipboard=True):
    """Після озвучки: завантажити в сховище, показати й скопіювати посилання, сповістити в Telegram.
    cfg/nc: налаштування сховища й сповіщень (за замовчуванням з settings.json; у хмарі з змінних середовища)."""
    cfg = cfg or storage_config()
    if not cfg:
        return None
    try:
        link = upload_book(cfg, zip_path)
    except Exception as e:
        print(f"Завантаження в сховище не вдалося: {e}")
        return None
    copied = copy_to_clipboard(link) if clipboard else False
    if os.environ.get("GITHUB_ACTIONS") == "true":     # журнал може бути публічним: посилання на книгу йде лише в Telegram
        print("\nКнигу завантажено в сховище (посилання в журнал не пишемо)\n")
    else:
        print(f"\nПосилання на книгу{' (скопійовано в буфер)' if copied else ''}:\n{link}\n")
    log_event("посилання: " + link.split("/")[2] + "/…")
    nc = nc or notify_config()
    if nc:
        try:
            with zipfile.ZipFile(zip_path) as z:
                man = json.loads(z.read("manifest.json"))
            dur = sum(c.get("duration", 0) for c in man.get("chapters", []))
            notify_ready(nc, man.get("title", ""), man.get("author", ""), link, dur, Path(zip_path).stat().st_size / 1048576)
            print("Сповіщення в Telegram надіслано.")
            log_event("сповіщення в Telegram надіслано")
        except Exception as e:
            print(f"Сповіщення в Telegram не вдалося: {e} (посилання вище чинне)")
            log_event(f"сповіщення не вдалося: {e}")
    return link


def ask_notify():
    print("\nСповіщення в Telegram про готову книгу. Дані зберігаються лише на цьому комп'ютері.")
    labels = [("url", "Адреса проєкту Supabase (https://….supabase.co)"),
              ("anon_key", "Публічний ключ anon (Project Settings -> API)"),
              ("upload_key", "Ключ програми (select upload_key from reader_settings)")]
    cfg = {k: input(f"{label}: ").strip() for k, label in labels}
    save_settings(notify=cfg)
    return notify_config()


# ---------- режим стеження за папкою ----------

def file_is_stable(path, wait=3):
    try:
        s1 = path.stat().st_size
        time.sleep(wait)
        return s1 > 0 and s1 == path.stat().st_size
    except OSError:
        return False


def watch(books_dir, out_dir, gender, rate, concurrency):
    books_dir, out_dir = Path(books_dir), Path(out_dir)
    books_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = {}   # книга -> (час останньої помилки, кількість помилок)
    print(f"Стежу за папкою: {books_dir}\nГотові книги: {out_dir}\nЩоб зупинити, закрийте вікно (Ctrl+C).")
    while True:
        for p in sorted(books_dir.glob("*.epub")):
            if (out_dir / f"{p.stem}.audiobook.zip").exists():
                continue
            if p in failed:
                when, n = failed[p]
                if n >= 3 or time.time() - when < 120:
                    continue
            if not file_is_stable(p):
                continue
            try:
                publish(convert(p, out_dir, gender, rate, concurrency))
            except Exception as e:
                print(f"\nПомилка з «{p.name}»: {e}")
                n = failed.get(p, (0, 0))[1] + 1
                failed[p] = (time.time(), n)
                if n < 3:
                    print("Спробую ще раз за 2 хвилини: уже озвучене збережено, робота продовжиться з місця зупинки.")
        print("\r  Чекаю на нові книги в папці... (перевірено о " + time.strftime("%H:%M:%S") + ")   ", end="", flush=True)
        time.sleep(15)


def find_icloud(home=None):
    """Шукає теку iCloud Drive: збережений шлях, потім типові місця в профілі Windows."""
    saved = load_settings().get("icloud") if home is None else None
    if saved and Path(saved).is_dir():
        return Path(saved)
    home = Path(home) if home else Path.home()
    for name in ("iCloudDrive", "iCloud Drive", "iCloud"):
        d = home / name
        if d.is_dir():
            return d
    try:
        for d in sorted(home.iterdir()):
            if d.is_dir() and d.name.lower().replace(" ", "").startswith("icloud"):
                return d
    except OSError:
        pass
    return None


def settings_path():
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Ozvuchka" / "settings.json"


def load_settings():
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(**kw):
    data = load_settings()
    data.update(kw)
    try:
        sp = settings_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_gender():
    """Голос запам'ятовується; при першому запуску запитуємо один раз."""
    g = load_settings().get("gender")
    if g in ("male", "female"):
        return g
    print("Який голос використовувати?\n  1 - жіночий\n  2 - чоловічий")
    try:
        ans = input("Введіть 1 або 2 і натисніть Enter [1]: ").strip()
    except EOFError:
        ans = ""
    gender = "male" if ans == "2" else "female"
    save_settings(gender=gender)
    return gender


def ask_icloud_path():
    """Якщо iCloud Drive не знайдено, просимо вставити шлях до теки (його видно в адресному рядку Провідника)."""
    print("Не знайдено теку iCloud Drive автоматично.\n"
          "Відкрийте iCloud Drive у Провіднику, клацніть у порожнє місце адресного рядка,\n"
          "скопіюйте шлях (Ctrl+C) і вставте сюди правою кнопкою миші.")
    try:
        ans = input("Шлях до iCloud Drive (або просто Enter, щоб вийти): ").strip().strip('"')
    except EOFError:
        return None
    if ans and Path(ans).is_dir():
        save_settings(icloud=ans)
        return Path(ans)
    if ans:
        print("Така тека не знайдена.")
    return None


_LOCK_HANDLE = None


def single_instance_lock():
    """Не дає запустити другу копію програми: дві копії псують спільні робочі файли. Повертає ручку або None, якщо вже запущено."""
    global _LOCK_HANDLE
    path = settings_path().parent / "running.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        return True   # не змогли створити файл блокування: не заважаємо роботі
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    _LOCK_HANDLE = fh
    return fh


def pause_if_exe():
    if getattr(sys, "frozen", False):
        try:
            input("\nНатисніть Enter, щоб закрити вікно...")
        except EOFError:
            pass


def easy_mode(files):
    """Режим «подвійний клік»: без параметрів стежить за iCloud, з файлами озвучує їх."""
    global LOG_FILE
    disable_quick_edit()
    print(f"Озвучка книг, версія {APP_VERSION}\n" + "=" * 24)
    try:
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE = settings_path().parent / "log.txt"
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        print(f"Журнал: {LOG_FILE}")
        log_event("запуск програми")
    except OSError:
        LOG_FILE = None
    if single_instance_lock() is None:
        print("Програма вже запущена в іншому вікні. Закрийте зайве вікно: дві копії одночасно псують роботу одна одній.")
        return
    gender = load_gender()
    if not storage_config() and not load_settings().get("storage_declined"):
        ans = input("Налаштувати завантаження книг за посиланням (замість iCloud)? [т/Enter = пропустити]: ").strip().lower()
        if ans in ("т", "y", "так", "yes"):
            ask_storage()
        else:
            save_settings(storage_declined=True)
    if storage_config() and not notify_config() and not load_settings().get("notify_declined"):
        ans = input("Налаштувати сповіщення в Telegram про готову книгу? [т/Enter = пропустити]: ").strip().lower()
        if ans in ("т", "y", "так", "yes"):
            ask_notify()
        else:
            save_settings(notify_declined=True)
    icloud = find_icloud()
    if storage_config() and not icloud and not files:
        icloud = Path.home() / "Озвучка"
        icloud.mkdir(exist_ok=True)
        print(f"Кладіть .epub у папку: {icloud / 'Книги'}")
    if not icloud and not files:
        icloud = ask_icloud_path()
    out_dir = None
    if icloud:
        books_dir = icloud / "Книги"
        out_dir = icloud / "Озвучено"
        books_dir.mkdir(exist_ok=True)
        out_dir.mkdir(exist_ok=True)
    if files:
        for f in files:
            target = out_dir if out_dir else Path(f).resolve().parent
            try:
                publish(convert(f, target, gender, "+0%", easy_jobs()))
            except Exception as e:
                print(f"Помилка з «{f}»: {e}")
        return
    if not icloud:
        print("Тоді перетягніть файл .epub на значок цієї програми: книга озвучиться поруч.")
        return
    watch(books_dir, out_dir, gender, "+0%", easy_jobs())


def main():
    ap = argparse.ArgumentParser(description="Озвучка EPUB в аудіокнигу для плеєра")
    ap.add_argument("epub", nargs="*", help="файли .epub")
    ap.add_argument("--watch", action="store_true", help="стежити за папкою з книгами")
    ap.add_argument("--books", help="папка з EPUB для --watch")
    ap.add_argument("--out", help="папка для готових книг")
    ap.add_argument("--male", action="store_true", help="чоловічі голоси замість жіночих")
    ap.add_argument("--rate", default="+0%", help="швидкість озвучки при генерації, напр. +10%% (в плеєрі швидкість змінюється окремо)")
    ap.add_argument("--jobs", type=int, default=3, help="скільки фрагментів озвучувати паралельно")
    ap.add_argument("--dry-run", action="store_true", help="показати розділи й мови без озвучки")
    args = ap.parse_args()

    only_files = args.epub and not (args.watch or args.books or args.out or args.male
                                    or args.dry_run or args.rate != "+0%" or args.jobs != 3)
    if only_files or not (args.epub or args.watch or args.dry_run):
        # просте використання: подвійний клік або перетягування файлів
        if "--help" not in sys.argv:
            try:
                easy_mode(args.epub)
            except KeyboardInterrupt:
                pass
            except Exception as e:
                print(f"\nПомилка: {e}")
            pause_if_exe()
            return

    gender = "male" if args.male else "female"
    icloud = find_icloud() or (Path.home() / "iCloudDrive")
    books_dir = Path(args.books) if args.books else icloud / "Книги"
    out_dir = Path(args.out) if args.out else icloud / "Озвучено"

    if args.dry_run:
        for f in args.epub:
            dry_run(f)
        return
    if args.watch:
        watch(books_dir, out_dir, gender, args.rate, args.jobs)
        return
    for f in args.epub:
        target = Path(args.out) if args.out else Path(f).resolve().parent
        try:
            convert(f, target, gender, args.rate, args.jobs)
        except Exception as e:
            print(f"Помилка з «{f}»: {e}")


if __name__ == "__main__":
    main()
