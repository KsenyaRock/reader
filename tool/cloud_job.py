#!/usr/bin/env python3
"""Хмарна озвучка (GitHub Actions): качає EPUB за посиланням, озвучує, кладе книгу в сховище й сповіщає в Telegram.
Усі секрети беруться зі змінних середовища (GitHub Secrets), у репозиторії їх немає.
Змінні: EPUB_URL, GENDER (female/male/apple), JOBS, R2_ENDPOINT, R2_BUCKET, R2_KEY_ID, R2_SECRET, R2_PUBLIC_BASE,
        SUPABASE_URL, SUPABASE_ANON_KEY, READER_UPLOAD_KEY (останні три необов'язкові), JOB_ID (задача з читалки, необов'язково).
Якщо EPUB лежить у нашому сховищі (R2_PUBLIC_BASE/in/...), після роботи його видаляють.
Голос Apple (GENDER=apple) іде в два етапи (STAGE): apple-synth на macOS (запис у теку APPLE_DIR) і apple-pack на Linux
(збірка книги з APPLE_DIR); вхідний EPUB видаляється лише після другого етапу або при помилці.
Або на кількох серверах: apple-shard (SHARD із SHARDS; по фрагменту say -> mp3 -> tmp/.../seg/ у сховищі; закінчивши свою
частку, сервер забирає ще не взяті фрагменти повільніших, позначки own/ і steal/), потім apple-join на Linux (збірка
книги, публікація, прибирання tmp/); apple-cleanup прибирає tmp/ і вхідний EPUB після збою.
Голоси Edge так само можна розподілити: edge-shard (SHARD із SHARDS), edge-join, edge-cleanup."""
import hashlib
import hmac
import http.client
import json
import os
import random
import sys
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audiobook_tool as T  # noqa: E402
import apple_voice as A  # noqa: E402

MAX_EPUB = 150 * 1024 * 1024


def download(url, dest):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.netloc:
        raise SystemExit("Посилання на EPUB має починатися з https://")
    req = urllib.request.Request(url, headers={"User-Agent": "reader-cloud-job"})
    got = 0
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            got += len(block)
            if got > MAX_EPUB:
                raise SystemExit("EPUB завеликий (понад 150 МБ)")
            out.write(block)
    if got < 1000:
        raise SystemExit("Завантажено порожній або надто малий файл")
    print(f"EPUB завантажено: {got // 1024} КБ")


def storage_from_env(env=os.environ):
    cfg = {"endpoint": env.get("R2_ENDPOINT", ""), "region": "auto", "bucket": env.get("R2_BUCKET", ""),
           "key_id": env.get("R2_KEY_ID", ""), "secret": env.get("R2_SECRET", ""), "public_base": env.get("R2_PUBLIC_BASE", "")}
    missing = [k for k in ("endpoint", "bucket", "key_id", "secret", "public_base") if not cfg[k]]
    if missing:
        raise SystemExit("Не задано секрети сховища: " + ", ".join(missing))
    return cfg


def notify_from_env(env=os.environ):
    nc = {"url": env.get("SUPABASE_URL", ""), "anon_key": env.get("SUPABASE_ANON_KEY", ""), "upload_key": env.get("READER_UPLOAD_KEY", "")}
    return nc if all(nc.values()) else None


def job_update(nc, job, status, err=None):
    """Повідомляє читалці про стан задачі (running/done/failed). Збій тут ніколи не ламає озвучку."""
    if not nc or not job:
        return
    body = json.dumps({"k": nc["upload_key"], "p_job": int(job), "p_status": status, "p_err": err}).encode("utf-8")
    req = urllib.request.Request(nc["url"].rstrip("/") + "/rest/v1/rpc/reader_job_update", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "apikey": nc["anon_key"],
                                          "Authorization": "Bearer " + nc["anon_key"]})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"Не вдалося оновити статус задачі: {e}")


def job_title(nc, job, title):
    """Підставляє справжню назву книги (з метаданих EPUB) у задачу. Збій тут ніколи не ламає озвучку."""
    if not nc or not job or not title or title == "Без назви":
        return
    body = json.dumps({"k": nc["upload_key"], "p_job": int(job), "p_title": title}).encode("utf-8")
    req = urllib.request.Request(nc["url"].rstrip("/") + "/rest/v1/rpc/reader_job_title", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "apikey": nc["anon_key"],
                                          "Authorization": "Bearer " + nc["anon_key"]})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"Не вдалося оновити назву задачі: {e}")


def s3_request(cfg, method, key, dest=None):
    """Підписаний запит без тіла до бакета (AWS SigV4): DELETE або GET (тоді відповідь пишеться у файл dest). Повертає код."""
    u = urllib.parse.urlparse(cfg["endpoint"])
    host, region = u.netloc, cfg.get("region") or "auto"
    uri = "/" + urllib.parse.quote(f"{cfg['bucket']}/{key}")
    now = time.gmtime()
    amz_date, day = time.strftime("%Y%m%dT%H%M%SZ", now), time.strftime("%Y%m%d", now)
    empty = hashlib.sha256(b"").hexdigest()
    headers = {"host": host, "x-amz-content-sha256": empty, "x-amz-date": amz_date}
    names = sorted(headers)
    canonical = method + "\n" + uri + "\n\n" + "".join(f"{n}:{headers[n]}\n" for n in names) + "\n" + ";".join(names) + "\n" + empty
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
    k = T._sign(("AWS4" + cfg["secret"]).encode(), day)
    for part in (region, "s3", "aws4_request"):
        k = T._sign(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={cfg['key_id']}/{scope}, SignedHeaders={';'.join(names)}, Signature={sig}"
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(host, timeout=120)
    try:
        conn.putrequest(method, uri, skip_host=True, skip_accept_encoding=True)
        for n, v in headers.items():
            conn.putheader(n, v)
        conn.endheaders()
        resp = conn.getresponse()
        if dest is not None and resp.status == 200:
            with open(dest, "wb") as out:
                for block in iter(lambda: resp.read(1 << 20), b""):
                    out.write(block)
        else:
            resp.read()
        return resp.status
    finally:
        conn.close()


def s3_delete(cfg, key):
    """Видаляє об'єкт із бакета (підпис AWS SigV4, порожнє тіло)."""
    return s3_request(cfg, "DELETE", key) in (200, 204)


def tmp_prefix(url):
    """Тимчасова тека в сховищі для частин однієї озвучки на кількох серверах. Обчислюється з посилання на EPUB
    (у ньому випадковий код), тому однакова на всіх серверах і не вгадується з публічного журналу."""
    return "tmp/" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def shards_from_env(env=os.environ):
    try:
        shards = max(1, min(20, int(env.get("SHARDS", "1"))))
        shard = int(env.get("SHARD", "0"))
    except ValueError:
        raise SystemExit("SHARD і SHARDS мають бути числами")
    if not 0 <= shard < shards:
        raise SystemExit("SHARD має бути від 0 до SHARDS-1")
    return shard, shards


def put_part(cfg, url, kind, shard, shards, part):
    """Кладе архів частини kind-<shard>.zip у тимчасову теку сховища (3 спроби)."""
    for attempt in range(1, 4):
        try:
            T.s3_put(cfg, f"{tmp_prefix(url)}/{kind}-{shard}.zip", part)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise RuntimeError(f"частину {shard + 1} не вдалося покласти в сховище: {e}")
            time.sleep(3 * attempt)
    print(f"Частину {shard + 1} з {shards} покладено в сховище ({part.stat().st_size // 1048576} МБ)", flush=True)


def get_parts(cfg, url, kind, shards, dest, work):
    """Забирає всі частини kind-*.zip і розпаковує в dest."""
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()
    for i in range(shards):
        f = work / f"{kind}-{i}.zip"
        st = s3_request(cfg, "GET", f"{tmp_prefix(url)}/{kind}-{i}.zip", dest=f)
        if st != 200:
            raise RuntimeError(f"частина {i + 1} з {shards} не знайдена в сховищі (код {st})")
        with zipfile.ZipFile(f) as z:
            z.extractall(dest)
        f.unlink()
    print(f"Частини забрано за {int(time.time() - started)} с", flush=True)


def edge_plan(epub, gender):
    """План Edge-озвучки (однаковий на кожному сервері): книга, частини, робоча тека, id, імена фрагментів по порядку."""
    epub = Path(epub)
    book = T.read_epub(epub)
    if not book["chapters"]:
        raise RuntimeError("У книзі не знайдено тексту (можливо, файл захищений DRM)")
    parts = T.plan_book(book, T.default_cyr_for(book))
    book_id = A.book_id_of(epub)
    workdir = T.work_root() / hashlib.sha1(f"{book_id}{gender}+0%".encode()).hexdigest()[:10]   # як у T.convert
    workdir.mkdir(parents=True, exist_ok=True)
    items = [(T.chunk_name(pi, ci), None, c["text"]) for pi, p in enumerate(parts) for ci, c in enumerate(p["chunks"])]
    return book, parts, workdir, book_id, items


def edge_shard(epub, gender, jobs, cfg, url, work):
    """Сервер SHARD із SHARDS (Linux): озвучує свою частку фрагментів Edge і кладе їх у тимчасову теку сховища."""
    import asyncio
    shard, shards = shards_from_env()
    book, parts, workdir, _, items = edge_plan(epub, gender)
    mine = {n for n, _, _ in A.shard_tasks(items, shard, shards)}
    chars = sum(len(t) for n, _, t in items if n in mine)
    print(f"«{T.pub(book['title'])}»: {len(items)} фрагментів, сервер {shard + 1} з {shards}: {len(mine)} фрагментів, "
          f"{chars} символів, паралельних запитів {jobs}", flush=True)
    started = time.time()
    asyncio.run(T.run_synthesis(parts, workdir, gender, "+0%", jobs, only=mine))
    print(f"Озвучено за {int(time.time() - started)} с, повторів {T.STATS.get('retries', 0)}", flush=True)
    part = work / f"edge-{shard}.zip"
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as z:
        for n in sorted(mine):
            z.write(workdir / n, n)
    put_part(cfg, url, "edge", shard, shards, part)


def edge_join(epub, gender, cfg, url, work):
    """Linux: забирає озвучені фрагменти з усіх серверів і збирає книгу. Повертає шлях до zip."""
    _, shards = shards_from_env()
    book, parts, workdir, book_id, _ = edge_plan(epub, gender)
    get_parts(cfg, url, "edge", shards, workdir, work)
    for pi, p in enumerate(parts):
        for ci, c in enumerate(p["chunks"]):
            c["path"] = workdir / T.chunk_name(pi, ci)
    missing = [str(c["path"].name) for p in parts for c in p["chunks"] if not c["path"].exists()]
    if missing:
        raise RuntimeError(f"бракує {len(missing)} озвучених фрагментів (перший: {missing[0]})")
    return T.assemble(book, parts, epub, work / "out", workdir, book_id)


def safe_reason(msg):
    """Категорія помилки для публічного журналу: без назв книг, посилань і уривків тексту."""
    if msg.startswith("сховище:"):
        return ": " + msg[:90]
    for prefix, label in (("голос ", "запис голосу"), ("не вдалося перетворити", "перетворення в mp3"),
                          ("фрагмент ", "збірка: бракує фрагмента"), ("частина ", "збірка: бракує частини"),
                          ("бракує ", "збірка: бракує фрагментів"), ("EPUB", "EPUB")):
        if msg.startswith(prefix):
            return ": " + label
    return ""


APPLE_SAY = "say"        # у тестах підміняється заглушкою
APPLE_FFMPEG = None      # None = imageio-ffmpeg або ffmpeg із системи


def s3_exists(cfg, key):
    """Чи є об'єкт у сховищі (HEAD); тимчасові збої мережі повторюються."""
    for attempt in range(1, 6):
        try:
            code = s3_request(cfg, "HEAD", key)
            if code in (200, 404):
                return code == 200
            err = f"код {code}"
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        time.sleep(min(2 ** attempt, 20) + random.random())
    raise RuntimeError(f"сховище: перевірка фрагмента не вдалася ({err})")


def seg_key(url, kind, name):
    """Ключі розподіленої Apple-озвучки: seg/<фрагмент>.mp3 (готовий звук), own/<фрагмент> (взяв свій сервер),
    steal/<фрагмент> (забрав інший сервер, що вже закінчив свою частку)."""
    return f"{tmp_prefix(url)}/{kind}/{name}"


def put_small(cfg, key, path):
    for attempt in range(1, 7):
        try:
            T.s3_put(cfg, key, path, content_type="application/octet-stream")
            return
        except Exception as e:  # noqa: BLE001
            if attempt == 6:
                code = "".join(ch for ch in str(e)[:40] if ch.isdigit())[:3]
                raise RuntimeError(f"сховище: не вдалося покласти фрагмент ({type(e).__name__} {code})".strip())
            time.sleep(min(2 ** attempt, 30) + random.random())


def steal_order(lists, shard):
    """Чужі фрагменти для «крадіжки»: з кінців черг інших серверів по черзі (їхні власники дійдуть туди найпізніше)."""
    others = [l for s, l in enumerate(lists) if s != shard]
    out = []
    for k in range(max((len(l) for l in others), default=0)):
        for l in others:
            if k < len(l):
                out.append(l[-1 - k])
    return out


def apple_shard(epub, apple_dir, cfg, url, work):
    """Сервер SHARD із SHARDS: спершу записує свою частку (по фрагменту: say -> mp3 -> сховище), потім забирає
    ще не взяті фрагменти з кінців черг повільніших серверів. Домовленість через позначки own/ і steal/ у сховищі
    (без бази); рідкісний подвійний запис одного фрагмента нешкідливий."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    shard, shards = shards_from_env()
    plan, tasks = A.build_plan(epub)
    lists = [A.shard_tasks(tasks, s, shards) for s in range(shards)]
    order = [(t, True) for t in lists[shard]] + [(t, False) for t in steal_order(lists, shard)]
    apple_dir, mp3dir = Path(apple_dir), work / "mp3"
    apple_dir.mkdir(parents=True, exist_ok=True)
    mp3dir.mkdir(parents=True, exist_ok=True)
    marker = work / "marker"
    marker.write_bytes(b"1")
    ffmpeg = APPLE_FFMPEG or A.find_ffmpeg()
    jobs = max(1, int(os.environ.get("APPLE_JOBS", "4")))
    lock, pos, stop = threading.Lock(), [0], []
    st = {"own": 0, "stolen": 0, "skipped": 0}
    print(f"«{T.pub(plan['title'])}»: {len(tasks)} сегментів, сервер {shard + 1} з {shards}: своїх {len(lists[shard])}, "
          f"паралельно {jobs}", flush=True)
    started = time.time()

    def take():
        with lock:
            if stop or pos[0] >= len(order):
                return None
            pos[0] += 1
            return order[pos[0] - 1]

    def worker():
        try:
            while True:
                it = take()
                if not it:
                    return
                (name, voice, text), mine = it
                mp3name = name[:-4] + ".mp3"
                if s3_exists(cfg, seg_key(url, "seg", mp3name)) or s3_exists(cfg, seg_key(url, "steal", name)) \
                        or (not mine and s3_exists(cfg, seg_key(url, "own", name))):
                    with lock:
                        st["skipped"] += 1
                    continue
                put_small(cfg, seg_key(url, "own" if mine else "steal", name), marker)
                m4a = A.record((name, voice, text), apple_dir, APPLE_SAY)
                mp3 = A.mp3_one(m4a, mp3dir / mp3name, ffmpeg)
                put_small(cfg, seg_key(url, "seg", mp3name), mp3)
                m4a.unlink()
                mp3.unlink()
                with lock:
                    st["own" if mine else "stolen"] += 1
                    n = st["own"] + st["stolen"]
                    if n % 10 == 0:
                        print(f"  готово {n} (своїх {st['own']}/{len(lists[shard])}, забрано в інших {st['stolen']}), "
                              f"минуло {int(time.time() - started)} с", flush=True)
        except BaseException:
            stop.append(1)
            raise

    with ThreadPoolExecutor(jobs) as ex:
        futs = [ex.submit(worker) for _ in range(jobs)]
    for f in futs:
        f.result()
    print(f"Сервер {shard + 1}: своїх {st['own']}, забрано в інших {st['stolen']}, пропущено (зробив інший) {st['skipped']}, "
          f"за {int(time.time() - started)} с", flush=True)


def apple_join(epub, cfg, url, work):
    """Linux: забирає готові mp3 усіх фрагментів зі сховища й збирає книгу. Повертає шлях до zip."""
    from concurrent.futures import ThreadPoolExecutor
    plan, tasks = A.build_plan(epub)
    got = work / "join"
    got.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def fetch(t):
        mp3name = t[0][:-4] + ".mp3"
        code = s3_request(cfg, "GET", seg_key(url, "seg", mp3name), dest=got / mp3name)
        if code != 200:
            raise RuntimeError(f"фрагмент {mp3name} не знайдено в сховищі (код {code})")
        return t[0], got / mp3name

    with ThreadPoolExecutor(8) as ex:
        mp3s = dict(ex.map(fetch, tasks))
    print(f"Фрагменти ({len(mp3s)}) забрано за {int(time.time() - started)} с", flush=True)
    book, book_id = T.read_epub(epub), A.book_id_of(epub)
    workdir = T.work_root() / ("apple_" + book_id)
    workdir.mkdir(parents=True, exist_ok=True)
    return A.assemble(plan, mp3s, book, epub, work / "out", workdir, book_id)


def apple_seg_cleanup(cfg, url, epub):
    """Прибирає позначки й фрагменти розподіленої Apple-озвучки (імена беруться з плану книги). Ніколи не падає."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        _, tasks = A.build_plan(epub)
    except Exception as e:  # noqa: BLE001
        print(f"План книги не прочитано, тимчасові файли прибере правило сховища: {type(e).__name__}")
        return
    keys = [seg_key(url, k, n) for t in tasks for k, n in (("seg", t[0][:-4] + ".mp3"), ("own", t[0]), ("steal", t[0]))]

    def rm(k):
        try:
            return s3_delete(cfg, k)
        except Exception:  # noqa: BLE001
            return False
    with ThreadPoolExecutor(8) as ex:
        n = sum(ex.map(rm, keys))
    print(f"Тимчасові файли прибрано: {n}", flush=True)


def apple_cleanup(cfg, url, kind="apple"):
    """Прибирає тимчасові частини озвучки (kind: apple або edge) зі сховища (після збірки або збою). Ніколи не падає."""
    _, shards = shards_from_env()
    n = 0
    for i in range(shards):
        try:
            n += s3_delete(cfg, f"{tmp_prefix(url)}/{kind}-{i}.zip")
        except Exception:  # noqa: BLE001
            pass
    print(f"Тимчасові частини прибрано: {n}", flush=True)


def input_key(cfg, url):
    """Ключ EPUB у нашому сховищі (лише в папці in/), інакше None."""
    base = cfg["public_base"].rstrip("/") + "/"
    if not url.startswith(base + "in/"):
        return None
    key = urllib.parse.unquote(url[len(base):])
    return key if ".." not in key else None


def run(url, gender, jobs, cfg, nc, job, stage=""):
    work = Path(tempfile.mkdtemp(prefix="cloud_job_"))
    os.environ.setdefault("OZVUCHKA_WORK", str(work / "work"))
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or "book.epub"
    if not name.lower().endswith(".epub"):
        name += ".epub"
    epub = work / name
    download(url, epub)
    apple_dir = Path(os.environ.get("APPLE_DIR", "apple-parts"))
    if stage == "apple-join":
        print("Збираю книгу з частин, записаних на кількох серверах")
        zip_path = apple_join(epub, cfg, url, work)
    elif stage == "edge-join":
        print("Збираю книгу з частин, озвучених на кількох серверах")
        zip_path = edge_join(epub, gender, cfg, url, work)
    elif stage == "apple-pack":
        print("Збираю книгу з записаного голосу Apple")
        zip_path = A.pack(apple_dir, epub, work / "out")
    else:
        try:
            job_title(nc, job, T.read_epub(epub)["title"])
        except Exception as e:  # noqa: BLE001
            print(f"Назву з EPUB не прочитано: {e}")
        if stage == "apple-synth":
            A.synth(epub, apple_dir, jobs=int(os.environ.get("APPLE_JOBS", "3")))
            return
        if stage == "apple-shard":
            apple_shard(epub, apple_dir, cfg, url, work)
            return
        if stage == "edge-shard":
            edge_shard(epub, gender, jobs, cfg, url, work)
            return
        out = work / "out"
        print(f"Озвучую: голос {'чоловічий' if gender == 'male' else 'жіночий'}, паралельних запитів {jobs}")
        zip_path = T.convert(epub, out, gender, "+0%", jobs)
    link = T.publish(zip_path, cfg=cfg, nc=nc, clipboard=False)
    if not link:
        raise SystemExit("Книгу озвучено, але завантажити в сховище не вдалося")
    if stage == "apple-join":
        apple_seg_cleanup(cfg, url, epub)
    elif stage == "edge-join":
        apple_cleanup(cfg, url, "edge")
    print("Готово.")


APPLE_STAGES = ("apple-synth", "apple-pack", "apple-shard", "apple-join", "apple-cleanup")
EDGE_STAGES = ("edge-shard", "edge-join", "edge-cleanup")


def main():
    url = os.environ.get("EPUB_URL", "").strip()
    if not url:
        raise SystemExit("Не вказано EPUB_URL")
    g = os.environ.get("GENDER", "female").lower()
    gender = g if g in ("male", "apple") else "female"
    stage = os.environ.get("STAGE", "").strip()
    if gender != "apple" and stage not in ("",) + EDGE_STAGES:
        raise SystemExit("Для голосів Edge STAGE має бути порожнім або edge-shard / edge-join / edge-cleanup")
    if (gender == "apple") != (stage in APPLE_STAGES):
        raise SystemExit("Голос Apple озвучується етапами: STAGE=apple-synth (macOS), потім STAGE=apple-pack; "
                         "або на кількох серверах: STAGE=apple-shard (SHARD, SHARDS), потім STAGE=apple-join")
    try:
        jobs = max(1, min(20, int(os.environ.get("JOBS", "12"))))
    except ValueError:
        jobs = 12
    job = os.environ.get("JOB_ID", "").strip() or None
    cfg = storage_from_env()
    if stage == "edge-cleanup":          # після збою: лише прибрати тимчасові частини
        apple_cleanup(cfg, url, "edge")
        return
    if stage == "apple-cleanup":         # після збою: прибрати фрагменти й позначки, потім вхідний EPUB
        work = Path(tempfile.mkdtemp(prefix="cloud_job_"))
        try:
            download(url, work / "book.epub")
            apple_seg_cleanup(cfg, url, work / "book.epub")
        except BaseException as e:  # noqa: BLE001
            print(f"EPUB для прибирання недоступний ({type(e).__name__}); тимчасові файли прибере правило сховища")
        key = input_key(cfg, url)
        if key:
            try:
                s3_delete(cfg, key)
            except Exception:  # noqa: BLE001
                pass
        return
    nc = notify_from_env()
    if stage not in ("apple-pack", "apple-join", "edge-join"):
        job_update(nc, job, "running")
    keep_input = stage in ("apple-synth", "apple-shard", "edge-shard")   # EPUB ще потрібен наступному етапу
    try:
        run(url, gender, jobs, cfg, nc, job, stage)
    except BaseException as e:  # noqa: BLE001  (SystemExit теж: текст причини показуємо в читалці)
        msg = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        job_update(nc, job, "failed", msg[:300])
        keep_input = stage == "apple-shard"   # EPUB потрібен кроку прибирання (він його й видалить)
        if os.environ.get("GITHUB_ACTIONS") == "true":   # журнал публічний: причина (з назвою або уривком) лише в читалці
            print(f"Озвучка перервалась ({type(e).__name__}{safe_reason(msg)}); подробиці в читалці")
            raise SystemExit(1) from None
        raise
    else:
        if stage not in ("apple-synth", "apple-shard", "edge-shard"):
            job_update(nc, job, "done")
    finally:
        key = None if keep_input else input_key(cfg, url)
        if key:
            try:
                print("Вхідний EPUB видалено зі сховища" if s3_delete(cfg, key) else "Вхідний EPUB не вдалося видалити")
            except Exception as e:  # noqa: BLE001
                print(f"Вхідний EPUB не вдалося видалити: {e}")


if __name__ == "__main__":
    main()
