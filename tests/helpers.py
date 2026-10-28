"""Спільні помічники тестів: збирання тестового EPUB і запуск справжньої програми озвучки з заглушкою."""
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tool", "audiobook_tool.py")
STUB = os.path.join(ROOT, "tests", "tool", "stub")

CONTAINER = ('<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')


def make_epub(path, chapters, title="Тестова книга", author="Автор", lang="uk", extra_items=None, files=None):
    """chapters: список (ім'я файлу відносно OEBPS/, html[, properties]). Порядок = spine."""
    items, refs = [], []
    for i, ch in enumerate(chapters):
        name = ch[0]
        props = f' properties="{ch[2]}"' if len(ch) > 2 else ""
        items.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"{props}/>')
        refs.append(f'<itemref idref="c{i}"/>')
    items += extra_items or []
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>{lang}</dc:language></metadata>'
           f'<manifest>{"".join(items)}</manifest><spine>{"".join(refs)}</spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for ch in chapters:
            z.writestr("OEBPS/" + ch[0], ch[1])
        for name, data in (files or {}).items():
            z.writestr("OEBPS/" + name, data)
    return path


def page(heading, *paras):
    body = "".join(f"<p>{p}</p>" for p in paras)
    h = f"<h1>{heading}</h1>" if heading else ""
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title><style>p{{color:red}}</style></head><body>{h}{body}</body></html>'


def run_tool(*args, env_extra=None):
    """Запускає справжню програму озвучки (з заглушкою edge_tts замість мережі)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = STUB
    env.update(env_extra or {})
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=env, timeout=300)
