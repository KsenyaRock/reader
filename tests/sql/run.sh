#!/usr/bin/env bash
# Перевірка sql/7, 8, 9, 18, 19 і 20 на чистому Postgres (двічі: ідемпотентність). PSQL="psql -h ... -U ... db" за потреби.
set -euo pipefail
cd "$(dirname "$0")/../.."
P=${PSQL:-psql}
$P -v ON_ERROR_STOP=1 -q -f tests/sql/stubs.sql
$P -v ON_ERROR_STOP=1 -q -f sql/7-supabase-reader.sql
$P -v ON_ERROR_STOP=1 -q -f sql/7-supabase-reader.sql
$P -v ON_ERROR_STOP=1 -q -f sql/8-supabase-reader-jobs.sql
$P -v ON_ERROR_STOP=1 -q -f sql/8-supabase-reader-jobs.sql
$P -v ON_ERROR_STOP=1 -q -f sql/9-supabase-reader-delete.sql
$P -v ON_ERROR_STOP=1 -q -f sql/9-supabase-reader-delete.sql
$P -v ON_ERROR_STOP=1 -q -f sql/18-supabase-reader-apple.sql
$P -v ON_ERROR_STOP=1 -q -f sql/18-supabase-reader-apple.sql
$P -v ON_ERROR_STOP=1 -q -f sql/19-supabase-reader-extras.sql
$P -v ON_ERROR_STOP=1 -q -f sql/19-supabase-reader-extras.sql
$P -v ON_ERROR_STOP=1 -q -f sql/20-supabase-reader-people.sql
$P -v ON_ERROR_STOP=1 -q -f sql/20-supabase-reader-people.sql
$P -v ON_ERROR_STOP=1 -q -f tests/sql/assert.sql
$P -v ON_ERROR_STOP=1 -q -f tests/sql/assert_jobs.sql
$P -v ON_ERROR_STOP=1 -q -f tests/sql/assert_people.sql
