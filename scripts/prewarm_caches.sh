#!/usr/bin/env bash
# WiNS Hub — pre-warm caches em endpoints de dashboard cold-expensive.
# TTL dos caches = 600s; este script roda a cada 5min via cron,
# garantindo que o rebuild (23s p/ matches_ouro, 4s p/ stats-public)
# nunca rode na frente de um usuario.
set -u
TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
for ep in \
  /api/dashboard/matches_ouro \
  /api/dashboard/stats-public \
  /api/dashboard/setores-public; do
  t=$(curl -s -o /dev/null --max-time 60 -w '%{time_total}' "http://localhost${ep}")
  echo "${TS} ${ep} ${t}s"
done
