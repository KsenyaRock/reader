"""Озвучка голосами Apple (Леся, Мілена, Samantha) у хмарі, у два етапи:
1) synth — на macOS-сервері GitHub: EPUB → частини (як для Edge) → сегменти однієї мови → команда say → AAC (.m4a) + plan.json.
   Хвилини macOS дорожчі в 10 разів, тому тут лише сам запис голосу, без жодних встановлень.
2) pack — на звичайному Linux-сервері: .m4a → mp3 (24 кГц, 48 кбіт/с, моно, як у Edge) → склеювання по частинах →
   .audiobook.zip у тому самому форматі, що й для Edge (обкладинка, назва й автор з EPUB)."""
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audiobook_tool as T  # noqa: E402

APPLE_VOICES = {"uk": "Lesya", "ru": "Milena", "en": "Samantha"}
SEG_CHARS = 4000        # один запуск say: ~4 хв звуку однією мовою (дрібно, щоб текст у плеєрі збігався зі звуком)
SAY_TIMEOUT = 1800      # секунд на один сегмент (з запасом: say пише в ~50 разів швидше за реальний час)
MIN_AUDIO = 1000        # байт: менше означає, що say нічого не записав


def segments(part, limit=SEG_CHARS):
    """Сусідні фрагменти однієї мови зливаються в сегменти до limit символів: [(lang, text)]."""
    out = []
    for c in part["chunks"]:
        if out and out[-1][0] == c["lang"] and len(out[-1][1]) + len(c["text"]) + 2 <= limit:
            out[-1] = (c["lang"], out[-1][1] + "\n\n" + c["text"])
        else:
            out.append((c["lang"], c["text"]))
    return out


def synth(epub, outdir, jobs=3, say="say"):
    """Етап 1 (Mac): записує сегменти в outdir/*.m4a і план outdir/plan.json. Повертає план."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    book = T.read_epub(epub)
    if not book["chapters"]:
        raise RuntimeError("У книзі не знайдено тексту (можливо, файл захищений DRM)")
    parts = T.plan_book(book, T.default_cyr_for(book))
    plan = {"title": book["title"], "author": book["author"], "parts": []}
    tasks = []
    for pi, p in enumerate(parts):
        files, texts = [], []
        for si, (lang, text) in enumerate(segments(p)):
            name = f"{pi:04d}_{si:03d}.m4a"
            files.append(name)
            texts.append(text)
            tasks.append((name, APPLE_VOICES[lang], text))
        plan["parts"].append({"title": p["title"], "files": files, "texts": texts})
    chars = sum(len(t[2]) for t in tasks)
    print(f"«{T.pub(book['title'])}»: {len(parts)} частин, {len(tasks)} сегментів, {chars} символів, паралельно {jobs}", flush=True)
    started, done = time.time(), {"n": 0, "chars": 0}

    def one(task):
        name, voice, text = task
        src = outdir / (name + ".txt")
        out = outdir / name
        src.write_text(text, encoding="utf-8")
        err = ""
        for attempt in range(1, 3):
            r = subprocess.run([say, "-v", voice, "-f", str(src), "-o", str(out), "--file-format=m4af", "--data-format=aac"],
                               capture_output=True, text=True, timeout=SAY_TIMEOUT)
            if r.returncode == 0 and out.exists() and out.stat().st_size >= MIN_AUDIO:
                break
            err = (r.stderr or "").strip()[:200] or f"код {r.returncode}"
            print(f"  повтор {name} ({voice}): {T.pub(err)}", flush=True)
        else:
            snippet = text[:60].replace("\n", " ")
            raise RuntimeError(f"голос {voice} не записав фрагмент «{snippet}»: {err}")
        src.unlink()
        done["n"] += 1
        done["chars"] += len(text)
        if done["n"] % 10 == 0 or done["n"] == len(tasks):
            el = time.time() - started
            print(f"  записано {done['n']}/{len(tasks)} ({done['chars'] * 100 // max(chars, 1)}%), минуло {int(el)} с", flush=True)

    with ThreadPoolExecutor(max(1, jobs)) as ex:
        list(ex.map(one, tasks))
    (outdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Запис голосу завершено за {int(time.time() - started)} с", flush=True)
    return plan


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def pack(segdir, epub, out_dir, ffmpeg=None, jobs=4):
    """Етап 2 (Linux): з сегментів і плану збирає .audiobook.zip. Повертає шлях до zip."""
    segdir, epub, out_dir = Path(segdir), Path(epub), Path(out_dir)
    plan = json.loads((segdir / "plan.json").read_text(encoding="utf-8"))
    ffmpeg = ffmpeg or find_ffmpeg()
    book = T.read_epub(epub)                 # обкладинка, назва, автор
    book_id = hashlib.sha1(f"{epub.stem}{epub.stat().st_size}".encode()).hexdigest()[:10]   # як у T.convert
    workdir = T.work_root() / ("apple_" + book_id)
    workdir.mkdir(parents=True, exist_ok=True)
    names = [f for p in plan["parts"] for f in p["files"]]
    missing = [n for n in names if not (segdir / n).exists()]
    if missing:
        raise RuntimeError(f"бракує {len(missing)} записаних фрагментів (перший: {missing[0]})")

    def conv(name):
        mp3 = workdir / (name[:-4] + ".mp3")
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(segdir / name), "-ac", "1", "-ar", "24000",
                            "-b:a", "48k", "-write_xing", "0", "-id3v2_version", "0", str(mp3)], capture_output=True, text=True)
        if r.returncode != 0 or not mp3.exists():
            raise RuntimeError(f"не вдалося перетворити {name} у mp3: {(r.stderr or '').strip()[:200]}")
        return mp3

    print(f"Перетворюю {len(names)} фрагментів у mp3...", flush=True)
    with ThreadPoolExecutor(max(1, jobs)) as ex:
        mp3s = dict(zip(names, ex.map(conv, names)))
    parts = [{"title": p["title"], "chunks": [{"path": mp3s[f], "text": (p.get("texts") or [""] * len(p["files"]))[k]}
                                              for k, f in enumerate(p["files"])]} for p in plan["parts"]]
    return T.assemble(book, parts, epub, out_dir, workdir, book_id)
