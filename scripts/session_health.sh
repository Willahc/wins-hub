#!/bin/bash
# WiNS Hub — Health check pré-sessão (Nível 6)
# Uso: bash /root/wins_hub/scripts/session_health.sh
OUT=/tmp/wins_session_ready.txt
echo "=== WiNS Hub Health $(date '+%Y-%m-%d %H:%M') ===" > $OUT

echo "" >> $OUT
echo "--- DB: tiers ---" >> $OUT
docker exec wins_hub-db-1 psql -U wins_app -d wins_hub -tA -c "
SELECT classificacao_computed, COUNT(*) n, ROUND(SUM(valor_estimado)/1e9,1) cap_bi
FROM obras WHERE motivo_invisivel IS NULL
GROUP BY 1 ORDER BY 2 DESC;" >> $OUT

echo "" >> $OUT
echo "--- DB: matches_obra_prestador (= matches_legacy, view) ---" >> $OUT
docker exec wins_hub-db-1 psql -U wins_app -d wins_hub -tA -c "
SELECT COUNT(*) total_matches, MAX(gerado_em) ultimo_match FROM matches_obra_prestador;" >> $OUT

echo "" >> $OUT
echo "--- DB: tier NULL (recompute pendente?) ---" >> $OUT
docker exec wins_hub-db-1 psql -U wins_app -d wins_hub -tA -c "
SELECT COUNT(*) obras_null_tier FROM obras
WHERE motivo_invisivel IS NULL AND classificacao_computed IS NULL;" >> $OUT

echo "" >> $OUT
echo "--- Disk VPS ---" >> $OUT
df -h /root | tail -1 >> $OUT

echo "" >> $OUT
echo "--- Backups recentes ---" >> $OUT
ls -lht /home/william/backups/ 2>/dev/null | head -5 >> $OUT

echo "" >> $OUT
echo "--- Cron ativo ---" >> $OUT
crontab -l 2>/dev/null | grep -v "^#" | grep -v "^$" >> $OUT

echo "" >> $OUT
echo "--- matches_obra_prestador stale? (>12h sem match novo = alerta) ---" >> $OUT
docker exec wins_hub-db-1 psql -U wins_app -d wins_hub -tA -c "
SELECT CASE WHEN MAX(gerado_em) < NOW() - INTERVAL '12 hours'
  THEN '🔴 STALE — cron pode estar parado'
  ELSE '✅ OK — matches frescos'
END AS status_matches FROM matches_obra_prestador;" >> $OUT

cat $OUT
