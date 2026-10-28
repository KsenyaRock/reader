#!/usr/bin/env python3
"""Хмарна озвучка (GitHub Actions): качає EPUB за посиланням, озвучує, кладе книгу в сховище й сповіщає в Telegram.
Усі секрети беруться зі змінних середовища (GitHub Secrets), у репозиторії їх немає.
Змінні: EPUB_URL, GENDER (female/male/apple), JOBS, R2_ENDPOINT, R2_BUCKET, R2_KEY_ID, R2_SECRET, R2_PUBLIC_BASE,
        SUPABASE_URL, SUPABASE_ANON_KEY, READER_UPLOAD_KEY (останні три необов'язкові), JOB_ID (задача з читалки, необов'язково).
Якщо EPUB лежить у нашому сховищі (R2_PUBLIC_BASE/in/...), після роботи його видаляють.
Голос Apple (GENDER=apple) іде в два етапи (STAGE): apple-synth на macOS (запис у теку APPLE_DIR) і apple-pack на Linux
(збірка книги з APPLE_DIR); вхідний EPUB видаляється лише після другого етапу або при помилці."""
import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
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


def s3_delete(cfg, key):
    """Видаляє об'єкт із бакета (підпис AWS SigV4, порожнє тіло)."""
    u = urllib.parse.urlparse(cfg["endpoint"])
    host, region = u.netloc, cfg.get("region") or "auto"
    uri = "/" + urllib.parse.quote(f"{cfg['bucket']}/{key}")
    now = time.gmtime()
    amz_date, day = time.strftime("%Y%m%dT%H%M%SZ", now), time.strftime("%Y%m%d", now)
    empty = hashlib.sha256(b"").hexdigest()
    headers = {"host": host, "x-amz-content-sha256": empty, "x-amz-date": amz_date}
    names = sorted(headers)
    canonical = "DELETE\n" + uri + "\n\n" + "".join(f"{n}:{headers[n]}\n" for n in names) + "\n" + ";".join(names) + "\n" + empty
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
    k = T._sign(("AWS4" + cfg["secret"]).encode(), day)
    for part in (region, "s3", "aws4_request"):
        k = T._sign(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={cfg['key_id']}/{scope}, SignedHeaders={';'.join(names)}, Signature={sig}"
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(host, timeout=60)
    try:
        conn.putrequest("DELETE", uri, skip_host=True, skip_accept_encoding=True)
        for n, v in headers.items():
            conn.putheader(n, v)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        return resp.status in (200, 204)
    finally:
        conn.close()


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
    if stage == "apple-pack":
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
        out = work / "out"
        print(f"Озвучую: голос {'чоловічий' if gender == 'male' else 'жіночий'}, паралельних запитів {jobs}")
        zip_path = T.convert(epub, out, gender, "+0%", jobs)
    link = T.publish(zip_path, cfg=cfg, nc=nc, clipboard=False)
    if not link:
        raise SystemExit("Книгу озвучено, але завантажити в сховище не вдалося")
    print("Готово.")


def main():
    url = os.environ.get("EPUB_URL", "").strip()
    if not url:
        raise SystemExit("Не вказано EPUB_URL")
    g = os.environ.get("GENDER", "female").lower()
    gender = g if g in ("male", "apple") else "female"
    stage = os.environ.get("STAGE", "").strip()
    if (gender == "apple") != (stage in ("apple-synth", "apple-pack")):
        raise SystemExit("Голос Apple озвучується в два етапи: STAGE=apple-synth (macOS), потім STAGE=apple-pack")
    try:
        jobs = max(1, min(20, int(os.environ.get("JOBS", "12"))))
    except ValueError:
        jobs = 12
    job = os.environ.get("JOB_ID", "").strip() or None
    cfg = storage_from_env()
    nc = notify_from_env()
    if stage != "apple-pack":
        job_update(nc, job, "running")
    keep_input = stage == "apple-synth"   # EPUB ще потрібен другому етапу
    try:
        run(url, gender, jobs, cfg, nc, job, stage)
    except BaseException as e:  # noqa: BLE001  (SystemExit теж: текст причини показуємо в читалці)
        msg = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        job_update(nc, job, "failed", msg[:300])
        keep_input = False
        if os.environ.get("GITHUB_ACTIONS") == "true":   # журнал публічний: причина (з назвою або уривком) лише в читалці
            print(f"Озвучка перервалась ({type(e).__name__}); подробиці в читалці")
            raise SystemExit(1) from None
        raise
    else:
        if stage != "apple-synth":
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
