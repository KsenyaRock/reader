#!/usr/bin/env bash
# Перевірка Edge Function reader-voice в bun з підробленим fetch (без мережі).
set -euo pipefail
cd "$(dirname "$0")/../.."
bun test tests/edge/reader-voice.test.ts
