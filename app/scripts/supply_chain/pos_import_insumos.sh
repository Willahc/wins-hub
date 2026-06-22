#!/usr/bin/env bash
#
# pos_import_insumos.sh
# ----------------------
# Pos-processamento do import de fornecedores de insumo (CNAE div 22/23/24 da RFB).
#
# PRE-REQUISITO: rode ANTES o /home/william/importar_insumos.sh <csv>
#   (ele carrega os fornecedores via staging + WHERE NOT EXISTS, status='importado_sc').
#
# O que este script faz:
#   (a) Re-materializa a cadeia (matches_cadeia_obra) chamando o materializador
#       dentro do container da API.
#   (b) Imprime o snapshot "DEPOIS" das MESMAS metricas do snapshot "antes":
#         - fornecedores ATIVAS (situacao_cadastral='02') por divisao_cnae 22/23/24
#         - linhas de matches_cadeia_obra com fornecedores_na_base>0 por
#           cnae_insumo_div 22/23/24
#       lado a lado com os numeros de referencia do "antes".
#
# Snapshot "ANTES" (capturado em 2026-06-22, pre-import):
#   - fornecedores ativas div 22/23/24 ........ 0 (zero linhas; o buraco a preencher)
#   - matches_cadeia_obra fornecedores_na_base>0
#     em cnae_insumo_div 22/23/24 ............. 0 (zero linhas)
#
# NAO muta o banco diretamente (apenas o materializador re-gera matches_cadeia_obra).
#
set -euo pipefail

DB="sudo docker exec wins_hub-db-1 psql -U wins_app -d wins_hub"

echo "=============================================================="
echo " POS-IMPORT INSUMOS  (rodar DEPOIS de importar_insumos.sh)"
echo "=============================================================="

echo
echo ">> (a) Re-materializando cadeia (matches_cadeia_obra)..."
sudo docker exec wins_hub-api-1 python /app/scripts/materializar_cadeia_obra.py
echo ">> Materializacao concluida."

echo
echo "=============================================================="
echo " SNAPSHOT DEPOIS"
echo "=============================================================="

echo
echo "--- fornecedores ATIVAS (situacao_cadastral='02') por divisao_cnae ---"
echo "    ANTES (ref 2026-06-22): div 22=0  | div 23=0  | div 24=0"
echo "    DEPOIS:"
$DB -c "SELECT divisao_cnae, COUNT(*) AS ativas
        FROM fornecedores
        WHERE situacao_cadastral='02' AND divisao_cnae IN ('22','23','24')
        GROUP BY divisao_cnae
        ORDER BY divisao_cnae;"

echo
echo "--- matches_cadeia_obra com fornecedores_na_base>0 por cnae_insumo_div ---"
echo "    ANTES (ref 2026-06-22): div 22=0  | div 23=0  | div 24=0"
echo "    DEPOIS:"
$DB -c "SELECT cnae_insumo_div, COUNT(*) AS linhas, SUM(fornecedores_na_base) AS soma_forn
        FROM matches_cadeia_obra
        WHERE fornecedores_na_base>0 AND cnae_insumo_div IN ('22','23','24')
        GROUP BY cnae_insumo_div
        ORDER BY cnae_insumo_div;"

echo
echo "=============================================================="
echo " NOTA DE COMPARACAO"
echo "=============================================================="
echo " - Esperado: ambas as metricas saem de 0 para >0 nas divisoes 22/23/24."
echo "   Se continuarem 0 -> o import (importar_insumos.sh) nao rodou ou o CSV"
echo "   nao trouxe ativas (situacao_cadastral='02') nessas divisoes."
echo " - Se as fornecedores ativas subiram mas matches_cadeia_obra continua 0,"
echo "   reveja o materializador / vinculo cnae_insumo_div <-> divisao_cnae."
echo "=============================================================="
