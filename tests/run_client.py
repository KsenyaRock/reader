#!/usr/bin/env python3
"""Запускає всі клієнтські тести (Playwright) і падає, якщо хоч один не пройшов."""
import glob, os, re, subprocess, sys
here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'client')
bad = 0
for f in sorted(glob.glob(os.path.join(here, 'run_*.py'))):
    name = os.path.basename(f)
    r = subprocess.run([sys.executable, f], capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    problems = []
    if r.returncode != 0: problems.append('exit %d' % r.returncode)
    problems += [l for l in out.splitlines() if re.match(r'^(FAIL\b|FAILED:|TEST FAILED)', l)]
    for l in out.splitlines():
        if l.startswith('ERRORS:') and l.strip() != 'ERRORS: []':
            problems.append('console errors: ' + l)
    print(('FAIL ' if problems else 'ok   ') + name)
    for p in problems:
        print('     ' + p)
    if problems:
        bad += 1; print(out[-2500:])
sys.exit(1 if bad else 0)
