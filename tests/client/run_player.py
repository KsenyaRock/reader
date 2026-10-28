#!/usr/bin/env python3
"""Інтерфейс плеєра в справжньому Chromium. Книга грає як один безперервний звук, розділи це позначки часу:
імпорт, швидкість, перехід між розділами без перемикання файлу, перемотка, таймер сну, збереження місця,
екран блокування (Media Session), журнал подій, офлайн.
Книгу створює справжня програма озвучки (з заглушкою edge_tts, що пише тихий MP3)."""
import functools, http.server, json, os, re, sys, tempfile, threading, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from helpers import make_epub, page, run_tool  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

fails = []
def check(name, ok, detail=''):
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else '  -> %s' % (detail,)))
    if not ok: fails.append(name)

VERSION = re.search(r'const VERSION = "([^"]+)"', open(os.path.join(ROOT, 'site', 'index.html'), encoding='utf8').read()).group(1)

tmp = tempfile.mkdtemp()
txt = lambda n, w: (w + ' ') * n
epub = make_epub(os.path.join(tmp, 'book.epub'), [
    ('c1.xhtml', page('Розділ перший', txt(60, 'Це початок нашої історії, він був у місті, де ніхто не знав його імені.'))),
    ('c2.xhtml', page('Розділ другий', txt(60, 'Місто було тихим, а вулиці порожніми, і вечір повільно спускався на дахи.'))),
    ('c3.xhtml', page('Розділ третій', txt(60, 'Наприкінці він нарешті зрозумів, чому ніхто в цьому місті не питав про минуле.'))),
], title='Тестова книга', author='Автор Тестовий')
r = run_tool(epub, '--out', tmp, env_extra={'STUB_TAGS': '1'})   # з тегами ID3, які програма має прибрати
assert r.returncode == 0, r.stdout + r.stderr
book_zip = os.path.join(tmp, 'book.audiobook.zip')
man = json.loads(zipfile.ZipFile(book_zip).read('manifest.json'))
CH = man['chapters']
OFF = [sum(c['duration'] for c in CH[:i]) for i in range(len(CH))]
TOTAL = sum(c['duration'] for c in CH)
check('програма озвучки зібрала 3 розділи по ~14 с', len(CH) == 3 and all(10 < c['duration'] < 20 for c in CH), CH)

bad_txt = os.path.join(tmp, 'x.zip'); open(bad_txt, 'w').write('це не архів')
deflated = os.path.join(tmp, 'deflated.zip')
with zipfile.ZipFile(deflated, 'w', zipfile.ZIP_DEFLATED) as z: z.writestr('manifest.json', json.dumps(man))
no_manifest = os.path.join(tmp, 'nomanifest.zip')
with zipfile.ZipFile(no_manifest, 'w', zipfile.ZIP_STORED) as z: z.writestr('a.mp3', b'x')

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k): pass
handler = functools.partial(Quiet, directory=os.path.join(ROOT, 'site'))
srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = 'http://127.0.0.1:%d/' % srv.server_address[1]

errors = []
AUDIO = "document.getElementById('audio')"
def js(p_, expr, arg=None): return p_.evaluate(expr) if arg is None else p_.evaluate(expr, arg)

SWIPE_JS = """([sel, dy]) => {
  const el = document.querySelector(sel);
  const mk = (type, y) => { const tc = new Touch({identifier: 1, target: el, clientX: 200, clientY: y});
    el.dispatchEvent(new TouchEvent(type, {bubbles: true, cancelable: true, touches: type === 'touchend' ? [] : [tc], changedTouches: [tc]})); };
  mk('touchstart', 300); mk('touchmove', 300 + dy / 2); mk('touchmove', 300 + dy); mk('touchend', 300 + dy);
}"""

def wrap_clicks(pg):
    orig = pg.click
    def click(sel, *a, **k):
        if sel.startswith('#chips') or sel in ('#faster', '#slower'):
            orig('#speedBtn'); orig(sel, *a, **k); orig('#sheetSpeed .bg', position={'x': 5, 'y': 5})
        else:
            orig(sel, *a, **k)
    pg.click = click

def label_is(pg, n): pg.wait_for_function("n => document.getElementById('pChapter').textContent.startsWith(n + ' з 3')", arg=str(n), timeout=8000)
def set_time(pg, t): pg.evaluate("t => { document.getElementById('audio').currentTime = t; }", t)
def now(pg): return js(pg, AUDIO + '.currentTime')
def open_book(pg, n):
    pg.click('.book'); label_is(pg, n); pg.wait_for_function(AUDIO + '.readyState >= 1')
    pg.wait_for_function("document.getElementById('playBtn').dataset.state === 'playing'", timeout=8000)   # тап по книзі одразу грає
    pg.click('#playBtn'); pg.wait_for_function("document.getElementById('playBtn').dataset.state === 'paused'")
def close(a, b, tol=0.4): return abs(a - b) < tol

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--autoplay-policy=no-user-gesture-required'])
    ctx = browser.new_context(viewport={'width': 390, 'height': 844})
    pg = ctx.new_page()
    wrap_clicks(pg)
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    pg.goto(URL)

    check('версія показана', pg.inner_text('#ver') == 'Читалка v' + VERSION, pg.inner_text('#ver'))
    check('порожня бібліотека', 'Поки порожньо' in pg.inner_text('#books'))

    for label, path, expect in [('не zip', bad_txt, 'Помилка'), ('стиснений zip', deflated, 'стиснений'), ('без manifest', no_manifest, 'manifest.json')]:
        pg.set_input_files('#fileInput', path)
        pg.wait_for_function("document.getElementById('importStatus').textContent.includes('Помилка')")
        st = pg.inner_text('#importStatus')
        check('імпорт відхиляє: ' + label, expect in st, st)

    # збій запису одного розділу: імпорт повторює спробу й успішно завершується
    pg.evaluate("""(() => { const orig = IDBObjectStore.prototype.put; let failed = false;
      IDBObjectStore.prototype.put = function (v, k) { if (!failed && String(k).endsWith('002.mp3')) { failed = true; throw new Error('тестовий збій'); } return orig.apply(this, arguments); }; })()""")
    pg.set_input_files('#fileInput', book_zip)
    pg.wait_for_function("document.getElementById('importStatus').textContent.includes('додано')", timeout=20000)
    check('імпорт повторює спробу після збою', 'спроба 1' in js(pg, "localStorage.getItem('readerLog')") and 'тестовий збій' in js(pg, "localStorage.getItem('readerLog')"))
    pg.set_input_files('#fileInput', book_zip)   # повторний імпорт пропускає вже додані розділи
    pg.wait_for_function("document.getElementById('importStatus').textContent.includes('додано')", timeout=20000)
    check('повторний імпорт не дублює книгу', pg.locator('.book').count() == 1, pg.locator('.book').count())
    check('книга з\'явилась у бібліотеці', pg.inner_text('.book .t') == 'Тестова книга' and 'Автор Тестовий' in pg.inner_text('.book .s'), pg.inner_text('.book'))

    # --- відкриття: одна безперервна доріжка ---
    open_book(pg, 1)
    check('плеєр відкрився', pg.is_visible('#player') and not pg.is_visible('#library'))
    check('список розділів', pg.locator('#chapters .chap').count() == 3)
    dur = js(pg, AUDIO + '.duration')
    check('звук книги один і дорівнює сумі розділів', close(dur, TOTAL, 0.6), (dur, TOTAL))
    check('екран блокування: метадані', js(pg, "navigator.mediaSession.metadata && navigator.mediaSession.metadata.album") == 'Тестова книга'
          and js(pg, "navigator.mediaSession.metadata.title") == 'Розділ перший')

    # --- швидкість ---
    pg.click('#chips button:has-text("2×")')
    check('швидкість 2×', js(pg, AUDIO + '.playbackRate') == 2 and pg.inner_text('#speedVal') == '2.0×', pg.inner_text('#speedVal'))
    pg.click('#faster')
    check('швидкість +0.05', abs(js(pg, AUDIO + '.playbackRate') - 2.05) < 1e-6, js(pg, AUDIO + '.playbackRate'))
    pg.click('#chips button:has-text("3×")')
    check('швидкість 3×', js(pg, AUDIO + '.playbackRate') == 3)
    pg.evaluate("(() => { const r = document.getElementById('speedRange'); r.value = 0.1; r.dispatchEvent(new Event('input')); })()")
    check('нижня межа 0.5×', js(pg, AUDIO + '.playbackRate') == 0.5, js(pg, AUDIO + '.playbackRate'))
    pg.evaluate("(() => { const r = document.getElementById('speedRange'); r.value = 9; r.dispatchEvent(new Event('input')); })()")
    check('верхня межа 3×', js(pg, AUDIO + '.playbackRate') == 3, js(pg, AUDIO + '.playbackRate'))

    # --- відтворення, пауза, перемотка ---
    pg.click('#chips button:has-text("1.5×")')
    pg.click('#playBtn')
    pg.wait_for_function(AUDIO + '.currentTime > 0.3', timeout=8000)
    check('грає', not js(pg, AUDIO + '.paused') and pg.get_attribute('#playBtn','data-state') == 'playing')
    pg.click('#playBtn')
    pg.wait_for_function("document.getElementById('playBtn').dataset.state === 'paused'")
    check('пауза', js(pg, AUDIO + '.paused'))
    set_time(pg, 2); pg.click('#back15')
    check('−15 с не йде нижче нуля', now(pg) < 0.05, now(pg))
    pg.click('#fwd30')
    check('+30 с перескакує через розділи', close(now(pg), 30, 0.5) and pg.inner_text('#pChapter').startswith('3 з 3'), (now(pg), pg.inner_text('#pChapter')))
    set_time(pg, 0); label_is(pg, 1)

    # --- перехід між розділами без перемикання файлу ---
    src0 = js(pg, AUDIO + '.src')
    pg.click('#chips button:has-text("3×")')
    set_time(pg, OFF[1] - 0.8)
    pg.click('#playBtn')
    label_is(pg, 2)
    pg.wait_for_timeout(300)
    check('межа розділів: звук не зупинився', not js(pg, AUDIO + '.paused'))
    check('межа розділів: той самий файл (src не змінився)', js(pg, AUDIO + '.src') == src0)
    check('межа розділів: час іде далі', now(pg) > OFF[1], now(pg))
    check('межа розділів: швидкість збережена', js(pg, AUDIO + '.playbackRate') == 3)
    check('межа розділів: метадані екрана блокування', js(pg, "navigator.mediaSession.metadata.title") == 'Розділ другий')
    pg.evaluate(AUDIO + '.pause()')

    # --- вибір розділу, ⏮ ⏭, повзунок ---
    pg.click('#chapBtn'); pg.click('#chapters .chap:nth-child(3)'); pg.click('#sheetChap .bg', position={'x': 5, 'y': 5})
    label_is(pg, 3)
    check('тап по розділу перемотує на його початок', close(now(pg), OFF[2], 0.6), (now(pg), OFF[2]))
    pg.evaluate(AUDIO + '.pause()')
    set_time(pg, OFF[2] + 0.2)
    pg.click('#prevCh'); label_is(pg, 2)
    check('⏮ на початку розділу веде в попередній', close(now(pg), OFF[1]), (now(pg), OFF[1]))
    set_time(pg, OFF[1] + 7)
    pg.click('#prevCh')
    check('⏮ посеред розділу веде на його початок', close(now(pg), OFF[1]) and pg.inner_text('#pChapter').startswith('2 з 3'), now(pg))
    pg.click('#nextCh'); label_is(pg, 3)
    check('⏭ веде в наступний розділ', close(now(pg), OFF[2]), (now(pg), OFF[2]))
    pg.click('#nextCh')
    check('⏭ на останньому розділі не ламається', pg.inner_text('#pChapter').startswith('3 з 3'))
    pg.evaluate("(() => { const r = document.getElementById('seek'); r.value = 500; r.dispatchEvent(new Event('change')); })()")
    check('повзунок перемотує в межах розділу', close(now(pg), OFF[2] + CH[2]['duration'] / 2, 0.6), now(pg))

    # --- таймер сну (хвилини) ---
    pg.click('#chips button:has-text("1×")')
    pg.click('#sleepBtn')
    check('таймер сну: вікно з варіантами', all(t in pg.inner_text('#sheetSleep') for t in ('5 хв', '30 хв', '60 хв', 'До кінця розділу')) and not pg.is_visible('#sleepOff'))
    pg.click('#sleepChips button:has-text("15 хв")')
    check('таймер сну: 15 хв у статусі', 'Зупиниться через' in pg.inner_text('#sleepStatus') and pg.inner_text('#sleepLbl') == '15 хв', pg.inner_text('#sleepStatus'))
    pg.click('#sleepBtn'); check('таймер сну: є «Вимкнути», коли він іде', pg.is_visible('#sleepOff'))
    pg.click('#sleepOff')
    check('таймер сну: «Вимкнути» знімає статус', pg.inner_text('#sleepStatus') == '' and pg.inner_text('#sleepLbl') == '')
    pg.evaluate('startSleep(0.05)')
    pg.click('#playBtn')
    pg.wait_for_function(AUDIO + '.paused === false')
    pg.wait_for_function(AUDIO + '.paused', timeout=8000)
    check('таймер сну зупиняє звук', js(pg, AUDIO + '.paused'))
    check('таймер сну знято, гучність повернена', pg.inner_text('#sleepStatus') == '' and js(pg, AUDIO + '.volume') == 1)

    # --- таймер сну «до кінця розділу» ---
    pg.click('#chapBtn'); pg.click('#chapters .chap:nth-child(1)'); pg.click('#sheetChap .bg', position={'x': 5, 'y': 5}); label_is(pg, 1)
    pg.evaluate(AUDIO + '.pause()')
    pg.click('#sleepBtn'); pg.click('#sleepChips button:has-text("До кінця розділу")')
    check('сон до кінця розділу: статус', 'наприкінці розділу' in pg.inner_text('#sleepStatus'), pg.inner_text('#sleepStatus'))
    pg.click('#chips button:has-text("3×")')
    set_time(pg, OFF[1] - 1.5)
    pg.click('#playBtn')
    pg.wait_for_function(AUDIO + '.paused === false')
    pg.wait_for_function(AUDIO + '.paused', timeout=8000)
    check('сон до кінця розділу: зупинка точно на межі', close(now(pg), OFF[1], 0.35), (now(pg), OFF[1]))
    label_is(pg, 2)
    check('сон до кінця розділу: статус знято', pg.inner_text('#sleepStatus') == '')

    # --- збереження місця ---
    pg.click('#chips button:has-text("1.5×")')
    set_time(pg, OFF[1] + 2)
    pg.click('#playBtn')
    pg.wait_for_function('t => document.getElementById("audio").currentTime > t', arg=OFF[1] + 3.2, timeout=8000)
    pg.click('#playBtn')                                   # пауза зберігає позицію
    pg.wait_for_function(AUDIO + '.paused')
    t_paused = now(pg)
    pg.wait_for_timeout(300)
    pg.reload()
    pg.wait_for_selector('.book')
    check('прогрес у бібліотеці', 'лишилось' in pg.inner_text('.book .s'), pg.inner_text('.book .s'))
    open_book(pg, 2)
    check('позиція після паузи відновилась', close(now(pg), t_paused, 0.35), (now(pg), t_paused))
    check('швидкість відновилась', js(pg, AUDIO + '.playbackRate') == 1.5, js(pg, AUDIO + '.playbackRate'))
    set_time(pg, OFF[2] + 1)                               # перемотка на паузі теж зберігається
    pg.wait_for_timeout(300)
    pg.reload()
    open_book(pg, 3)
    check('позиція після перемотки на паузі відновилась', close(now(pg), OFF[2] + 1, 0.35), now(pg))

    # --- старий формат позиції {ch, t} (v0.1) ---
    pg.click('#backBtn'); pg.wait_for_selector('.book')
    pg.evaluate("""new Promise(r => { const q = indexedDB.open('reader'); q.onsuccess = () => {
        const t = q.result.transaction('books', 'readwrite'), s = t.objectStore('books');
        s.getAll().onsuccess = e => { const b = e.target.result[0]; b.pos = {ch: 1, t: 2}; s.put(b); };
        t.oncomplete = () => r(); }; })""")
    pg.reload(); pg.wait_for_selector('.book')
    open_book(pg, 2)
    check('старий формат позиції підхоплюється', close(now(pg), OFF[1] + 2, 0.5), now(pg))

    # --- кінець книги ---
    pg.click('#chips button:has-text("3×")')
    set_time(pg, TOTAL - 1.2)
    pg.click('#playBtn')
    pg.wait_for_function(AUDIO + '.ended === true', timeout=8000)
    pg.wait_for_timeout(300)
    pg.click('#backBtn'); pg.wait_for_selector('.book')
    open_book(pg, 1)
    check('після кінця книги вона відкривається з початку', now(pg) < 1, now(pg))

    # --- свайп вниз закриває плеєр ---
    pg.evaluate(SWIPE_JS, ['#art', 40]); pg.wait_for_timeout(300)
    check('короткий свайп плеєр не закриває', pg.is_visible('#player'))
    pg.evaluate(SWIPE_JS, ['#seek', 300]); pg.wait_for_timeout(300)
    check('свайп по повзунку плеєр не закриває', pg.is_visible('#player'))
    pg.evaluate(SWIPE_JS, ['#art', 300]); pg.wait_for_selector('#library', state='visible', timeout=5000)
    check('довгий свайп вниз повертає до бібліотеки', not pg.is_visible('#player'))
    pg.click('.book'); pg.wait_for_selector('#player', state='visible')

    # --- назад не зупиняє звук, є міні-плеєр ---
    pg.wait_for_function(AUDIO + '.currentTime > 0.3', timeout=8000)
    pg.click('#backBtn'); pg.wait_for_selector('#library', state='visible')
    check('після «назад» звук грає далі', not js(pg, AUDIO + '.paused'))
    check('міні-плеєр видно в бібліотеці', pg.is_visible('#mini'))
    pg.click('#miniBtn'); pg.wait_for_function(AUDIO + '.paused')
    pg.wait_for_function("document.getElementById('mini').dataset.state === 'paused'", timeout=3000)
    check('кнопка міні-плеєра ставить на паузу', True)
    pg.click('#mini .info'); pg.wait_for_selector('#player', state='visible')
    check('тап по міні-плеєру відкриває плеєр без перезавантаження', not pg.is_visible('#mini'))
    pg.click('#backBtn'); pg.wait_for_selector('#library', state='visible')
    pg.click('.book'); pg.wait_for_selector('#player', state='visible')
    pg.wait_for_function("document.getElementById('playBtn').dataset.state === 'playing'", timeout=8000)
    check('тап по книзі в списку відразу відтворює', True)

    # --- журнал подій ---
    pg.click('#backBtn'); pg.wait_for_selector('.book')
    pg.click('#ver')
    log_text = pg.input_value('#diagText')
    check('журнал відкривається тапом на версію', pg.is_visible('#diag'))
    check('журнал містить події', all(w in log_text for w in ['play', 'pause', 'розділ 2/3', 'loadedmetadata']), log_text[-400:])
    pg.click('#diagClear')
    check('журнал очищується', pg.input_value('#diagText') == '')
    pg.click('#ver')
    check('журнал закривається', not pg.is_visible('#diag'))

    # --- офлайн: оболонка з кешу service worker ---
    pg.evaluate("navigator.serviceWorker.ready.then(() => true)")
    pg.reload(); pg.wait_for_function("navigator.serviceWorker.controller !== null", timeout=8000)
    ctx.set_offline(True)
    pg.reload()
    check('офлайн: сторінка відкривається з кешу', pg.is_visible('#library') and 'Читалка v' in pg.inner_text('#ver'))
    check('офлайн: книга на місці (IndexedDB)', pg.locator('.book').count() == 1)
    open_book(pg, 1)
    pg.click('#playBtn')
    pg.wait_for_function(AUDIO + '.currentTime > 0.3', timeout=8000)
    check('офлайн: книга грає', not js(pg, AUDIO + '.paused'))
    pg.click('#playBtn')
    pg.click('#backBtn'); pg.wait_for_selector('.book')
    ctx.set_offline(False)

    # --- видалення ---
    pg.click('.book')
    pg.wait_for_selector('#player', state='visible')
    pg.once('dialog', lambda dlg: dlg.accept())
    pg.click('#moreBtn'); pg.click('#delBtn')
    pg.wait_for_selector('#library', state='visible')
    pg.wait_for_function("document.getElementById('books').textContent.includes('Поки порожньо')", timeout=5000)
    check('видалення книги', 'Поки порожньо' in pg.inner_text('#books'))
    left = js(pg, "new Promise(r => { const q = indexedDB.open('reader'); q.onsuccess = () => { const t = q.result.transaction('blobs'); const c = t.objectStore('blobs').count(); c.onsuccess = () => r(c.result); }; })")
    check('видалення прибирає аудіо з IndexedDB', left == 0, left)
    check('видалення відпускає звуковий потік', js(pg, AUDIO + '.getAttribute("src")') is None)

    # --- обкладинка з фото ---
    import base64
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
    cover_png = os.path.join(tmp, 'cover.png'); open(cover_png, 'wb').write(png)
    pg.set_input_files('#fileInput', book_zip)
    pg.wait_for_function("document.getElementById('importStatus').textContent.includes('додано')", timeout=20000)
    pg.click('.book'); pg.wait_for_selector('#player', state='visible')
    pg.click('#moreBtn'); pg.set_input_files('#coverInput', cover_png)
    pg.wait_for_function("document.getElementById('art').style.background.includes('blob:')", timeout=8000)
    check('обкладинка з фото показується в плеєрі', True)
    pg.wait_for_function("navigator.mediaSession.metadata && navigator.mediaSession.metadata.artwork[0].src.startsWith('blob:')", timeout=5000)
    check('обкладинка на екрані блокування', True)
    pg.click('#backBtn')
    pg.wait_for_function("(document.querySelector('.book .art')||{style:{background:''}}).style.background.includes('blob:')", timeout=5000)
    check('обкладинка в бібліотеці', 'blob:' in js(pg, "document.querySelector('.book .art').style.background"))
    pg.reload(); pg.wait_for_selector('.book')
    check('обкладинка переживає перезапуск', 'blob:' in js(pg, "document.querySelector('.book .art').style.background"))
    pg.click('.book'); pg.wait_for_selector('#player', state='visible')
    pg.click('#moreBtn'); pg.once('dialog', lambda dlg: dlg.accept()); pg.click('#delBtn')
    pg.wait_for_selector('#library', state='visible')

    # --- додавання за посиланням ---
    body = open(book_zip, 'rb').read()
    def serve(route):
        req = route.request
        h = {'content-type': 'application/zip', 'access-control-allow-origin': '*', 'access-control-allow-headers': 'range', 'accept-ranges': 'bytes'}
        if req.method == 'OPTIONS':
            return route.fulfill(status=204, headers=dict(h, **{'access-control-allow-methods': 'GET,HEAD'}))
        if req.method == 'HEAD':
            return route.fulfill(status=200, headers=dict(h, **{'content-length': str(len(body))}))
        m = re.match(r'bytes=(\d+)-(\d+)', req.headers.get('range', ''))
        ranges.append(req.headers.get('range'))
        if not m: return route.fulfill(status=200, body=body, headers=h)
        a, b = int(m.group(1)), int(m.group(2))
        route.fulfill(status=206, body=body[a:b + 1], headers=h)
    ranges = []
    pg.route('https://files.example/book.audiobook.zip', serve)
    pg.route('https://files.example/missing.zip', lambda route: route.fulfill(status=404, body='no', headers={'access-control-allow-origin': '*'}))
    pg.evaluate("importFromUrl('http://x/y.zip')")
    check('посилання без https відхиляється', 'https' in pg.inner_text('#importStatus'))
    pg.evaluate("importFromUrl('https://files.example/missing.zip')")
    pg.wait_for_function("document.getElementById('importStatus').textContent.includes('Не вдалося')", timeout=8000)
    check('помилка сервера показується', '404' in pg.inner_text('#importStatus'), pg.inner_text('#importStatus'))
    pg.evaluate("importFromUrl('https://files.example/book.audiobook.zip')")
    try:
        pg.wait_for_function("document.getElementById('importStatus').textContent.includes('додано')", timeout=15000)
    except Exception:
        print('STATUS:', pg.inner_text('#importStatus'), ranges[:5]); raise
    check('книга додається за посиланням', pg.locator('.book').count() == 1)
    open_book(pg, 1)
    pg.click('#playBtn')
    pg.wait_for_function(AUDIO + '.currentTime > 0.3', timeout=8000)
    check('книга читалась шматками, а не цілим файлом', len(ranges) >= 4 and all(r for r in ranges), ranges)
    check('книга з посилання грає', not js(pg, AUDIO + '.paused'))
    # --- текст книги ---
    check('кнопка «Текст» є, бо в архіві є text.json', pg.is_visible('#textBtn'))
    pg.click('#textBtn'); pg.wait_for_selector('#textList p')
    check('текст відкрився на поточному розділі', pg.inner_text('#textTitle').startswith('1/3') and pg.locator('#textList p').count() >= 2, pg.inner_text('#textTitle'))
    pg.wait_for_function("document.querySelector('#textList p.cur')", timeout=5000)
    check('підсвічено абзац, що звучить', pg.locator('#textList p.cur').count() == 1)
    fs0 = js(pg, "parseFloat(getComputedStyle(document.getElementById('textList')).fontSize)")
    pg.click('#textBigger')
    check('A+ збільшує шрифт і запам\'ятовує', js(pg, "parseFloat(getComputedStyle(document.getElementById('textList')).fontSize)") > fs0 and js(pg, "localStorage.getItem('textSize')") == str(int(fs0) + 2))
    pg.click('#textSmaller'); pg.click('#textSmaller')
    check('A− зменшує шрифт', js(pg, "parseFloat(getComputedStyle(document.getElementById('textList')).fontSize)") < fs0)
    pg.click('#textNext')
    check('› показує наступний розділ без перемотування', pg.inner_text('#textTitle').startswith('2/3') and js(pg, 'chapterAt(' + AUDIO + '.currentTime)') == 0)
    pg.locator('#textList p').nth(1).click()
    pg.wait_for_function('chapterAt(' + AUDIO + '.currentTime) === 1', timeout=5000)
    check('тап по абзацу перемотує на його час', close(now(pg), OFF[1], 15) and now(pg) >= OFF[1] - 0.5, now(pg))
    pg.wait_for_function("document.querySelector('#textList p.cur')", timeout=5000)
    check('після перемотування підсвічується цей розділ', pg.inner_text('#textTitle').startswith('2/3'))
    set_time(pg, OFF[2] + 1)
    pg.wait_for_function("document.getElementById('textTitle').textContent.startsWith('3/3')", timeout=6000)
    check('текст іде за звуком у наступний розділ', True)
    pg.click('#sheetText .bg', position={'x': 5, 'y': 5})
    check('вікно тексту закривається', not pg.is_visible('#sheetText .body'))

    # --- озвучка в хмарі: код, вибір EPUB, завантаження, запуск, список готових ---
    API = 'https://hikzqzdulvekuplolymw.supabase.co/functions/v1/reader-voice'
    cors = {'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*'}
    vcalls = []; puts = []; extra_on = [False]; people_on = [False]; prog = {'pos': 38, 'updated': '2020-01-01T00:00:00Z'}
    def api(route):
        req = route.request
        if req.method == 'OPTIONS': return route.fulfill(status=204, headers=cors)
        b = json.loads(req.post_data); vcalls.append(b)
        h = dict(cors, **{'content-type': 'application/json'})
        if b['code'] != 'good-code': return route.fulfill(status=401, headers=h, body=json.dumps({'ok': False, 'error': 'Неправильний код'}))
        if b['action'] == 'presign': return route.fulfill(status=200, headers=h, body=json.dumps({'ok': True, 'put_url': 'https://r2.example/put?sig=1', 'key': 'in/' + 'a' * 32 + '/book.epub'}))
        if b['action'] == 'run': return route.fulfill(status=200, headers=h, body=json.dumps({'ok': True, 'job': 5}))
        if b['action'] == 'list':
            jobs = [{'id': 5, 'title': 'Тестова книга', 'status': 'running', 'error': None}] if any(c['action'] == 'run' for c in vcalls) and not any(c['action'] == 'cancel' for c in vcalls) else []
            deleted = any(c['action'] == 'delete' for c in vcalls)
            books = ([{'id': 2, 'title': 'Нова книга', 'author': 'Хтось', 'link': 'https://files.example/new.audiobook.zip', 'duration_sec': 3600, 'size_mb': 30}] if extra_on[0] else []) + ([] if deleted else [{'id': 1, 'title': 'Готова книга', 'author': 'Автор', 'link': 'https://files.example/book.audiobook.zip', 'duration_sec': 40000, 'size_mb': 300}])
            out = {'ok': True, 'jobs': jobs, 'books': books}
            if people_on[0]:
                out['people'] = [{'key': 'anton', 'label': 'Олег'}, {'key': 'ksenia', 'label': 'Анна'}]
                out['books'] = books + [{'id': 3, 'title': 'Приватна', 'author': '', 'link': 'https://files.example/p.audiobook.zip', 'duration_sec': 600, 'size_mb': 5, 'audience': 'ksenia'}]
            return route.fulfill(status=200, headers=h, body=json.dumps(out))
        if b['action'] == 'progress_get': return route.fulfill(status=200, headers=h, body=json.dumps({'ok': True, 'progress': prog}))
        route.fulfill(status=200, headers=h, body=json.dumps({'ok': True}))
    def put(route):
        if route.request.method == 'OPTIONS': return route.fulfill(status=204, headers=cors)
        puts.append(route.request.method); route.fulfill(status=200, headers=cors, body='')
    pg.route(API, api); pg.route('https://r2.example/put**', put)
    pg.reload(); pg.wait_for_selector('.book')
    pg.evaluate("(() => { window.__sent = []; const os = XMLHttpRequest.prototype.send; XMLHttpRequest.prototype.send = function (b) { window.__sent.push(b && b.size); return os.apply(this, arguments); }; })()")
    pg.click('#addBtn')
    check('без коду показується поле коду', pg.is_visible('#voiceCode') and not pg.is_visible('#voicePick'))
    pg.fill('#voiceCode', 'wrong'); pg.click('#voiceSave')
    pg.wait_for_function("document.getElementById('toast').textContent.includes('Неправильний')", timeout=5000)
    check('неправильний код не зберігається', js(pg, "localStorage.getItem('voiceCode')") in (None, ''))
    pg.fill('#voiceCode', 'good-code'); pg.click('#voiceSave')
    pg.wait_for_selector('#voicePick', state='visible')
    check('правильний код зберігається й відкриває екран озвучки', js(pg, "localStorage.getItem('voiceCode')") == 'good-code')
    pg.wait_for_selector('#voiceBooks .vrow')
    check('список «Готові в хмарі»', 'Готова книга' in pg.inner_text('#voiceBooks') and '11 год 6 хв' in pg.inner_text('#voiceBooks'), pg.inner_text('#voiceBooks'))
    notepub = os.path.join(tmp, 'not.txt'); open(notepub, 'w').write('x' * 2000)
    pg.set_input_files('#voiceFile', notepub)
    pg.wait_for_function("document.getElementById('voiceStatus').textContent.includes('.epub')", timeout=5000)
    check('не-EPUB відхиляється', len([c for c in vcalls if c['action'] == 'presign']) == 0)
    pg.click('#vgM')
    dlg_msgs = []
    pg.once('dialog', lambda dlg: (dlg_msgs.append(dlg.message), dlg.dismiss()))
    pg.set_input_files('#voiceFile', epub)
    pg.wait_for_timeout(800)
    check('перед запуском показується оцінка й можна відмовитись', len(dlg_msgs) == 1 and 'звуку' in dlg_msgs[0] and 'хв безкоштовного ліміту' in dlg_msgs[0]
          and not any(c['action'] in ('presign', 'run') for c in vcalls), dlg_msgs)
    pg.once('dialog', lambda dlg: dlg.accept())
    pg.set_input_files('#voiceFile', epub)
    pg.wait_for_function("document.getElementById('voiceStatus').textContent.includes('запущено')", timeout=10000)
    run = [c for c in vcalls if c['action'] == 'run'][0]
    pre = [c for c in vcalls if c['action'] == 'presign'][0]
    check('EPUB завантажено у сховище за підписаним посиланням', puts == ['PUT'] and js(pg, 'window.__sent') == [os.path.getsize(epub)] and pre['size'] == os.path.getsize(epub), (puts, pre))
    check('запуск із чоловічим голосом і назвою файлу', run['gender'] == 'male' and run['title'] == 'book.epub' and run['key'].endswith('/book.epub'), run)
    pg.wait_for_function("document.getElementById('voiceJobs').textContent.includes('озвучується')", timeout=8000)
    check('активна задача видна у списку', True)
    pg.wait_for_selector('#voiceJobs .vrow button')
    pg.once('dialog', lambda dlg: dlg.dismiss())
    pg.click('#voiceJobs .vrow button'); pg.wait_for_timeout(300)
    check('відмова від скасування нічого не скасовує', not any(c['action'] == 'cancel' for c in vcalls))
    pg.once('dialog', lambda dlg: dlg.accept())
    pg.click('#voiceJobs .vrow button')
    pg.wait_for_function("document.getElementById('voiceJobs').children.length === 0", timeout=8000)
    cc = [c for c in vcalls if c['action'] == 'cancel']
    check('скасування озвучки: запит із id задачі, список оновився', len(cc) == 1 and cc[0]['id'] == 5, cc)
    pg.reload(); pg.wait_for_selector('.book')
    pg.click('#addBtn'); pg.wait_for_selector('#voicePick', state='visible')
    check('код і голос пам\'ятаються після перезапуску', js(pg, "localStorage.getItem('voiceGender')") == 'male')
    check('підказка з голосами для вибраного варіанта', 'Остап' in pg.inner_text('#voiceHint'))
    pg.click('#vgA')
    check('кнопка «Голос Apple» вибирається й запам\'ятовується', 'on' in pg.get_attribute('#vgA', 'class') and 'on' not in pg.get_attribute('#vgM', 'class')
          and js(pg, "localStorage.getItem('voiceGender')") == 'apple' and 'Леся' in pg.inner_text('#voiceHint'))
    pg.click('#vgM')
    pg.wait_for_selector('#voiceBooks .vrow button'); pg.click('#voiceBooks .vrow button')
    pg.wait_for_function("document.getElementById('importStatus').textContent.includes('додано')", timeout=15000)
    check('«Додати» з готових імпортує книгу за посиланням', not pg.is_visible('#sheetVoice .body'))
    pg.click('#addBtn'); pg.wait_for_selector('#voiceBooks .vrow')
    check('біля «Додати» є «Видалити»', pg.locator('#voiceBooks .vrow button').count() == 2 and pg.locator('#voiceBooks .vrow button').nth(1).inner_text() == 'Видалити')
    pg.once('dialog', lambda dlg: dlg.dismiss())
    pg.locator('#voiceBooks .vrow button').nth(1).click(); pg.wait_for_timeout(300)
    check('скасування підтвердження нічого не видаляє', not any(c['action'] == 'delete' for c in vcalls))
    pg.once('dialog', lambda dlg: dlg.accept())
    pg.locator('#voiceBooks .vrow button').nth(1).click()
    pg.wait_for_function("document.getElementById('voiceBooks').children.length === 0", timeout=8000)
    dc = [c for c in vcalls if c['action'] == 'delete']
    check('видалення з хмари: запит із id книги, список оновився', len(dc) == 1 and dc[0]['id'] == 1, dc)
    sk = pg.evaluate("(() => { const b = {skipped: [{title: 'Зміст', why: 'позначено в книзі як toc'}, 'Примітки']}; return b.skipped.map(x => typeof x === 'string' ? x : '«' + x.title + '» (' + x.why + ')').join('; '); })()")
    check('формат пропущених розділів: об\'єкти й рядки', sk == '«Зміст» (позначено в книзі як toc); Примітки', sk)
    # --- нові книги в хмарі (плашка) і синхронізація позиції ---
    pg.click('#sheetVoice .bg', position={'x': 5, 'y': 5})
    extra_on[0] = True
    pg.evaluate("cloudCheckAt = 0; checkCloudBooks(true)")
    pg.wait_for_selector('#newBooks .vrow')
    check('плашка пропонує лише нову книгу (вже додану не показує)', pg.locator('#newBooks .vrow').count() == 1 and 'Нова книга' in pg.inner_text('#newBooks'), pg.inner_text('#newBooks'))
    check('нічого не додається само', pg.locator('.book').count() == 1)
    pg.locator('#newBooks .vrow button').nth(1).click()
    check('«Не потрібна» ховає плашку й запам\'ятовує', pg.locator('#newBooks .vrow').count() == 0 and 'new.audiobook.zip' in js(pg, "localStorage.getItem('dismissedCloud')"))
    pg.evaluate("cloudCheckAt = 0; checkCloudBooks(true)"); pg.wait_for_timeout(500)
    check('відхилена книга більше не з\'являється', pg.locator('#newBooks .vrow').count() == 0)
    pg.evaluate("localStorage.removeItem('dismissedCloud')")
    pg.route('https://files.example/new.audiobook.zip', lambda route: route.fulfill(status=404, body='no', headers={'access-control-allow-origin': '*'}))
    pg.evaluate("cloudCheckAt = 0; checkCloudBooks(true)")
    pg.wait_for_selector('#newBooks .vrow')
    pg.locator('#newBooks .vrow button').nth(0).click()
    pg.wait_for_function("document.getElementById('importStatus').textContent.length > 0", timeout=8000)
    check('«Додати» на плашці запускає імпорт за посиланням', pg.locator('#newBooks .vrow').count() == 0)
    extra_on[0] = False
    pg.evaluate("localStorage.setItem('profile', 'Тест')")
    prog['pos'] = 38
    n0 = len([c for c in vcalls if c['action'] == 'progress_get'])
    pg.evaluate("bookDone = () => 0")
    msgs = []
    pg.once('dialog', lambda dlg: (msgs.append(dlg.message), dlg.accept()))
    pg.click('.book')
    pg.wait_for_function("document.getElementById('pChapter').textContent.startsWith('3 з 3')", timeout=8000)
    check('синхронізація: питає про перехід на іншу позицію', len(msgs) == 1 and 'Тест' in msgs[0] and '0:38' in msgs[0], msgs)
    check('синхронізація: запит позиції з профілем і книгою', any(c['action'] == 'progress_get' and c['profile'] == 'Тест' for c in vcalls[n0:]))
    pg.wait_for_function("document.getElementById('playBtn').dataset.state === 'playing'", timeout=8000)   # тап по книзі одразу грає
    pg.click('#playBtn'); pg.wait_for_function("document.getElementById('playBtn').dataset.state === 'paused'")
    pg.wait_for_timeout(300)
    check('синхронізація: позиція відправляється на паузі', any(c['action'] == 'progress_set' and c['profile'] == 'Тест' and c['pos'] >= 30 for c in vcalls), [c for c in vcalls if c['action'] == 'progress_set'])
    pg.evaluate("localStorage.removeItem('profile')")
    pg.reload(); pg.wait_for_selector('.book')
    pg.click('#addBtn'); pg.wait_for_selector('#voicePick', state='visible')
    # --- люди: «хто ви» і «для кого книга» ---
    people_on[0] = True
    pg.evaluate("localStorage.removeItem('me'); localStorage.removeItem('voiceFor'); localStorage.removeItem('people'); voiceRefresh()")
    pg.wait_for_selector('#whoBox', state='visible', timeout=8000)
    check('спершу питає, хто ви', pg.locator('#whoChips button').count() == 2 and not pg.is_visible('#forBox'))
    nrun = len([c for c in vcalls if c['action'] == 'run'])
    pg.click('#voicePick'); pg.wait_for_timeout(300)
    check('без вибору «хто ви» озвучку не запускає', len([c for c in vcalls if c['action'] == 'run']) == nrun and pg.is_visible('#whoBox'))
    pg.locator('#whoChips button', has_text='Анна').click()
    check('вибір запам\'ятовується разом із профілем синхронізації', js(pg, "localStorage.getItem('me')") == 'ksenia' and js(pg, "localStorage.getItem('profile')") == 'Анна')
    pg.wait_for_selector('#forBox', state='visible')
    texts = [pg.locator('#forChips button').nth(i).inner_text() for i in range(pg.locator('#forChips button').count())]
    check('кнопки «для кого»: я, інший, обоє', texts == ['Тільки я', 'Тільки Олег', 'Обоє'], texts)
    check('за замовчуванням «Обоє»', 'on' in pg.locator('#forChips button').nth(2).get_attribute('class'))
    check('кнопка профілю показує, хто ви', pg.inner_text('#voiceProfile') == 'Ви: Анна')
    pg.wait_for_function("document.getElementById('voiceBooks').textContent.includes('Приватна')", timeout=8000)
    check('біля книг показано, для кого вони', 'тільки для вас' in pg.inner_text('#voiceBooks'), pg.inner_text('#voiceBooks'))
    check('запити списку несуть «хто питає»', any(c['action'] == 'list' and c.get('me') == 'ksenia' for c in vcalls))
    pg.locator('#forChips button').nth(0).click()
    check('вибір «Тільки я» запам\'ятовується', js(pg, "localStorage.getItem('voiceFor')") == 'ksenia')
    dm = []
    pg.once('dialog', lambda dlg: (dm.append(dlg.message), dlg.accept()))
    pg.set_input_files('#voiceFile', epub)
    pg.wait_for_function("document.getElementById('voiceStatus').textContent.includes('запущено')", timeout=10000)
    runs = [c for c in vcalls if c['action'] == 'run']
    check('запуск несе «для кого» (мені)', runs[-1].get('for') == 'ksenia', runs[-1])
    check('в оцінці перед запуском видно, для кого', dm and 'тільки ви' in dm[0], dm)
    pg.locator('#forChips button').nth(2).click()
    check('«Обоє» запам\'ятовується', js(pg, "localStorage.getItem('voiceFor')") == 'both')
    pg.click('#voiceProfile')
    check('«Ви: …» дозволяє змінити, хто ви', pg.is_visible('#whoBox') and not pg.is_visible('#forBox'))
    pg.locator('#whoChips button', has_text='Олег').click()
    check('після зміни «для кого» скидається на «Обоє»', js(pg, "localStorage.getItem('me')") == 'anton' and js(pg, "localStorage.getItem('voiceFor')") == 'both')
    people_on[0] = False
    pg.evaluate("localStorage.removeItem('me'); localStorage.removeItem('people'); localStorage.removeItem('profile')")
    pg.click('#voiceChange')
    check('«Змінити код» повертає до введення', pg.is_visible('#voiceCode') and js(pg, "localStorage.getItem('voiceCode')") in (None, ''))
    pg.click('#sheetVoice .bg', position={'x': 5, 'y': 5})
    browser.close()

real = [e for e in errors if 'Failed to load resource' not in e]
print('ERRORS:', real)
if fails: print('FAILED:', fails); sys.exit(1)
print('усе гаразд')
