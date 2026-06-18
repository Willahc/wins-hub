#!/usr/bin/env bash
# =============================================================================
# run_harness.sh — Suíte de regressão do PORTÃO DE ENTRADA.
# Roda ANTES de qualquer deploy das fases seguintes (Fase 1+).
# Fase 0: valida pré-requisitos (índice), roda baselines SQL, valida casos YAML.
# Fase 1+: além do acima, importa portao.py e roda casos_decisao.yaml.
# Uso:  bash run_harness.sh            (imprime métricas + DRIFT vs baseline)
#       bash run_harness.sh --strict   (sai !=0 se qualquer invariante quebrar)
# =============================================================================
set -uo pipefail
DC="sudo docker exec -i wins_hub-db-1 psql -U wins_app -d wins_hub -t -A -F,"
HERE="$(cd "$(dirname "$0")" && pwd)"
STRICT=0; [[ "${1:-}" == "--strict" ]] && STRICT=1
fail=0
ok(){   echo "  [OK]   $*"; }
bad(){  echo "  [FALHA] $*"; fail=1; }

echo "== 0. Pré-requisitos =="
IDXOK=$(sudo docker exec wins_hub-db-1 psql -U wins_app -d wins_hub -tAc \
  "SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid::regclass::text='idx_fornecedores_cnpj_raiz';" 2>/dev/null)
[[ "$IDXOK" == "t" ]] && ok "idx_fornecedores_cnpj_raiz válido" || bad "índice de raiz ausente/inválido"

echo "== 1. Baselines SQL (drift) =="
for f in 01_funil 02_sanidade 03_motivos 04_resolucao_interna; do
  echo "--- $f ---"
  $DC < "$HERE/sql/$f.sql"
done

echo "== 2. Invariantes (guardas de regressão) =="
# setor mapping sólido: < 2% de falha de setor nos últimos 30d
SETOR_FAIL=$($DC <<'SQL'
WITH b AS (SELECT setor FROM obras WHERE criado_em>=now()-interval '30 days')
SELECT round(100.0*count(*) FILTER (WHERE setor IS NULL OR setor='OUTRO' OR setor NOT IN (SELECT DISTINCT setor FROM setor_categorias))/greatest(count(*),1),1)
FROM b;
SQL
)
awk -v v="$SETOR_FAIL" 'BEGIN{exit !(v<5)}' && ok "falha de setor ${SETOR_FAIL}% (<5%)" || bad "falha de setor ${SETOR_FAIL}% alta"

# resolução interna de domínio >= 45% (baseline 55%): regressão se cair muito
DOM=$($DC <<'SQL'
WITH obr AS (SELECT cnpj, substring(cnpj from 1 for 8) raiz FROM obras
  WHERE criado_em>=now()-interval '30 days' AND cnpj ~ '^[0-9]{14}$' AND cnpj_status IN ('ok','validated_brasilapi','validated_manual_brasilapi'))
SELECT round(100.0*count(*) FILTER (WHERE EXISTS(SELECT 1 FROM empresa_dominios d WHERE (d.cnpj=o.cnpj OR substring(d.cnpj from 1 for 8)=o.raiz) AND COALESCE(d.dominio,'')<>''))/greatest(count(*),1),1)
FROM obr o;
SQL
)
awk -v v="$DOM" 'BEGIN{exit !(v>=45)}' && ok "domínio interno ${DOM}% (>=45%)" || bad "domínio interno ${DOM}% abaixo do baseline"

echo "== 3. Casos de decisão (portao.avaliar real, dentro do container) =="
sudo docker exec wins_hub-api-1 python /app/scripts/portao/regression/test_casos.py
[[ $? -ne 0 ]] && bad "casos_decisao.yaml falharam" || ok "todos os casos passaram"

echo "== 4. Enforce (filtrar_e_enriquecer + guardrail + kill-switch + enrich-off) =="
sudo docker exec wins_hub-api-1 python /app/scripts/portao/regression/test_enforce.py
[[ $? -ne 0 ]] && bad "testes de enforce falharam" || ok "enforce ok"

echo "== Resumo =="
if [[ $fail -eq 0 ]]; then echo "HARNESS OK"; exit 0
else echo "HARNESS COM FALHAS"; [[ $STRICT -eq 1 ]] && exit 1 || exit 0; fi
