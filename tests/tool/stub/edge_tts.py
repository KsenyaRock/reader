"""Заглушка бібліотеки edge_tts для автотестів: замість мережі пише коректний тихий MP3.
Тривалість пропорційна довжині тексту. Журнал викликів: файл зі змінної STUB_LOG.
STUB_FLAKY=<каталог>: перша спроба для кожного тексту падає (перевірка повторів).
STUB_FAIL=1: усі спроби падають.
STUB_NOAUDIO=<фрагмент тексту>: для текстів із цим фрагментом Edge TTS «не повертає звук», як зі справжнім сервісом.
STUB_DEAD_VOICE=<голос>: цей голос «не повертає звук» на будь-який текст."""
import asyncio
import hashlib
import os

FRAME = b"\xff\xf3\x64\xc0" + b"\x00" * 140   # MPEG-2 Layer III, 24 кГц, 48 кбіт/с, моно: 144 байти ≈ 24 мс (як у edge-tts)


def silent_mp3(seconds):
    return FRAME * max(2, int(seconds / 0.024))


ACTIVE, MAX_ACTIVE = {}, {}   # одночасні запити на голос (для перевірки лімітів)


class Communicate:
    def __init__(self, text, voice, rate="+0%"):
        self.text, self.voice, self.rate = text, voice, rate

    async def save(self, path):
        log = os.environ.get("STUB_LOG")
        if log:
            with open(log, "a", encoding="utf8") as f:
                f.write(f"{self.voice}\t{self.rate}\t{self.text[:30]!r}\n")
        noaudio = os.environ.get("STUB_NOAUDIO")
        if (noaudio and noaudio in self.text) or os.environ.get("STUB_DEAD_VOICE") == self.voice:
            raise RuntimeError("No audio was received. Please verify that your parameters are correct.")
        if os.environ.get("STUB_FAIL"):
            raise RuntimeError("заглушка: мережа недоступна")
        flaky = os.environ.get("STUB_FLAKY")
        if flaky:
            mark = os.path.join(flaky, hashlib.md5(self.text.encode()).hexdigest())
            if not os.path.exists(mark):
                open(mark, "w").close()
                raise RuntimeError("заглушка: разова помилка")
        ACTIVE[self.voice] = ACTIVE.get(self.voice, 0) + 1
        MAX_ACTIVE[self.voice] = max(MAX_ACTIVE.get(self.voice, 0), ACTIVE[self.voice])
        await asyncio.sleep(0.03)
        ACTIVE[self.voice] -= 1
        seconds = max(1.0, len(self.text) / 300)   # 300 символів ≈ 1 с (у реальності ≈ 15 символів/с)
        tags = os.environ.get("STUB_TAGS")   # =1: додати ID3v2 на початок і ID3v1 у кінець, як роблять деякі кодери
        data = silent_mp3(seconds)
        if tags:
            data = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + data + b"TAG" + b"\x00" * 125
        with open(path, "wb") as f:
            f.write(data)
