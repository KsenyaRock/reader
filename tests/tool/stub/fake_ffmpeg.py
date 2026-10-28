#!/usr/bin/env python3
"""Заглушка ffmpeg для тестів: з «m4a» робить тихий mp3 (24 кГц, 48 кбіт/с, моно), тривалість пропорційна розміру входу."""
import os
import sys

FRAME = b"\xff\xf3\x64\xc0" + b"\x00" * 140
a = sys.argv[1:]
src, out = a[a.index("-i") + 1], a[-1]
frames = max(2, os.path.getsize(src) // 400)
with open(out, "wb") as f:
    f.write(FRAME * frames)
