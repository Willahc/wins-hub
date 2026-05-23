#!/usr/bin/env bash
# Refresh obras.score_prospeccao_cached pra todos os obras visiveis.
# Roda em ~3-4s sobre 27K rows. Cron diario 01:30 UTC.
set -u
LOG=/var/log/wins_hub_score_prospeccao.log
exec >>"$LOG" 2>&1
echo "==== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===="
docker exec wins_hub-db-1 psql -U postgres -d wins_hub -c "
UPDATE obras SET score_prospeccao_cached = (
 (CASE WHEN COALESCE(obras.cnpj_status,'') = 'ok' THEN 1 ELSE 0 END)
 + (CASE WHEN EXISTS (SELECT 1 FROM decisores_cache dc WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND dc.status_cnpj = 'ATIVA') THEN 1 ELSE 0 END)
 + (CASE WHEN EXISTS (SELECT 1 FROM decisores_cache dc WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND dc.socios IS NOT NULL AND jsonb_typeof(dc.socios) = 'array' AND jsonb_array_length(dc.socios) > 0) THEN 1 ELSE 0 END)
 + (CASE WHEN EXISTS (SELECT 1 FROM empresa_dominios ed WHERE ed.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND ed.dominio_status IN ('ok','validado','validado_holding','validado_rebrand_em_curso')) THEN 1 ELSE 0 END)
 + (CASE WHEN EXISTS (SELECT 1 FROM decisores_cache dc WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND dc.emails IS NOT NULL AND jsonb_typeof(dc.emails) = 'array' AND jsonb_array_length(dc.emails) > 0) THEN 1 ELSE 0 END)
);
"
echo "==== fim $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===="
