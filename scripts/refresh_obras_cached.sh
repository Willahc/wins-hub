#!/usr/bin/env bash
# Refresh nightly de TODAS as colunas cached em obras:
#   - score_prospeccao_cached     (0-5, 5 EXISTS materializados)
#   - tem_decisor_externo_cached  (BOOLEAN)
#   - is_ouro_decisor_cached      (BOOLEAN)
#   - decisor_replicado_fp_cached (BOOLEAN)
#
# Cron 01:30 UTC. Roda em ~5-10s sobre 27K rows.
# Substitui refresh_score_prospeccao.sh (mantido como fallback).
set -u
LOG=/var/log/wins_hub_obras_cached_refresh.log
exec >>"$LOG" 2>&1
echo "==== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===="
docker exec wins_hub-db-1 psql -U postgres -d wins_hub -c "
UPDATE obras SET
  score_prospeccao_cached = (
   (CASE WHEN COALESCE(obras.cnpj_status,'') = 'ok' THEN 1 ELSE 0 END)
   + (CASE WHEN EXISTS (SELECT 1 FROM decisores_cache dc WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND dc.status_cnpj = 'ATIVA') THEN 1 ELSE 0 END)
   + (CASE WHEN EXISTS (SELECT 1 FROM decisores_cache dc WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND dc.socios IS NOT NULL AND jsonb_typeof(dc.socios) = 'array' AND jsonb_array_length(dc.socios) > 0) THEN 1 ELSE 0 END)
   + (CASE WHEN EXISTS (SELECT 1 FROM empresa_dominios ed WHERE ed.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND ed.dominio_status IN ('ok','validado','validado_holding','validado_rebrand_em_curso')) THEN 1 ELSE 0 END)
   + (CASE WHEN EXISTS (SELECT 1 FROM decisores_cache dc WHERE dc.cnpj = regexp_replace(COALESCE(obras.cnpj,''),'[^0-9]','','g') AND dc.emails IS NOT NULL AND jsonb_typeof(dc.emails) = 'array' AND jsonb_array_length(dc.emails) > 0) THEN 1 ELSE 0 END)
  ),
  tem_decisor_externo_cached = EXISTS (
    SELECT 1 FROM decisores_obra d WHERE d.obra_id = obras.id AND d.excluido_em IS NULL
  ),
  is_ouro_decisor_cached = EXISTS (
    SELECT 1 FROM decisores_obra d
    WHERE d.obra_id = obras.id AND d.excluido_em IS NULL
      AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'
      AND ((COALESCE(d.email,'') <> '') OR (COALESCE(d.linkedin_url,'') <> ''))
  ),
  decisor_replicado_fp_cached = EXISTS (
    SELECT 1 FROM decisores_obra dob
    WHERE dob.obra_id = obras.id
      AND dob.nome = obras.nivel1_nome
      AND dob.hipotese_replicacao = 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
      AND dob.excluido_em IS NULL
  );
"
echo "==== fim $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===="
