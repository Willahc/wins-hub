#!/usr/bin/env bash
# WiNS Hub — one-shot: aplica pg_stat_statements via restart do container db.
# Idempotente: se ja' estiver carregado, sai sem fazer nada.
#
# ALTER SYSTEM ja' foi rodado em 2026-05-23 19:30 UTC (postgresql.auto.conf
# tem shared_preload_libraries='pg_stat_statements'). Esta script so' aplica
# o restart pra essa config tomar efeito.
set -u
LOG=/var/log/wins_hub_pg_stat_statements_enable.log
exec >>"$LOG" 2>&1
echo "==== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===="

current=$(docker exec wins_hub-db-1 psql -U postgres -d wins_hub -tAc "SHOW shared_preload_libraries;" 2>/dev/null || echo "ERR")
echo "shared_preload_libraries antes do restart: '${current}'"

if echo "$current" | grep -q pg_stat_statements; then
  echo "Ja' carregado — no-op. Saindo."
  exit 0
fi

echo "Conexoes ativas:"
docker exec wins_hub-db-1 psql -U postgres -d wins_hub -c "SELECT COUNT(*) FROM pg_stat_activity WHERE state != 'idle';" 2>&1 || true

echo "Restarting wins_hub-db-1..."
docker restart wins_hub-db-1
echo "Aguardando healthy..."
for i in $(seq 1 30); do
  s=$(docker inspect -f '{{.State.Health.Status}}' wins_hub-db-1 2>/dev/null)
  if [ "$s" = "healthy" ]; then echo "healthy em ${i}s"; break; fi
  sleep 1
done

echo "Verificacao:"
docker exec wins_hub-db-1 psql -U postgres -d wins_hub -c "SHOW shared_preload_libraries;"
docker exec wins_hub-db-1 psql -U postgres -d wins_hub -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
docker exec wins_hub-db-1 psql -U postgres -d wins_hub -c "SELECT COUNT(*) AS stmts_capturados FROM pg_stat_statements;"
echo "==== fim $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===="
