#!/usr/bin/env bash
# WiNS Hub — regenera arquivos .gz pre-comprimidos pra nginx gzip_static.
# Rodar APÓS deploy/edição de qualquer .js/.css/.html servido.
# Idempotente.
set -eu
ROOT=/root/wins_hub_v2/app/frontend
echo "Regenerating .gz files in $ROOT..."

# .js + .css
for f in "$ROOT"/static/js/*.js "$ROOT"/static/css/*.css; do
  [ -f "$f" ] || continue
  case "$f" in
    *.bak_*) continue ;;  # skip backup files
    *.min) continue ;;
  esac
  gzip -9 -kfc "$f" > "${f}.gz"
done

# index.html + outras HTMLs
for f in "$ROOT"/index.html "$ROOT"/login.html "$ROOT"/reset.html "$ROOT"/score.html "$ROOT"/esqueci.html; do
  [ -f "$f" ] && gzip -9 -kfc "$f" > "${f}.gz"
done

echo "Done — $(find "$ROOT" -name '*.gz' -not -name '*.bak*' 2>/dev/null | wc -l) .gz files"
