#!/usr/bin/env python3
"""Заглушка macOS-команди say для тестів: «записує» файл, розмір якого пропорційний тексту.
Журнал (голос і початок тексту): STUB_LOG. FAKE_SAY_FAIL=<голос>: цей голос завжди падає."""
import os
import sys

a = sys.argv[1:]
voice, src, out = a[a.index("-v") + 1], a[a.index("-f") + 1], a[a.index("-o") + 1]
text = open(src, encoding="utf-8").read()
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{voice}\tsay\t{text[:30]!r}\n")
if os.environ.get("FAKE_SAY_FAIL") == voice:
    sys.stderr.write("Voice not found\n")
    sys.exit(1)
with open(out, "wb") as f:
    f.write(b"M4A" + b"\x00" * max(2000, len(text.encode("utf-8")) * 4))
