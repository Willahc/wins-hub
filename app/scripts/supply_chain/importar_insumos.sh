#!/bin/bash
# importar_insumos.sh — importa o CSV filtrado (saída do etl_rfb_insumos_PC.py) em fornecedores.
# Uso: bash importar_insumos.sh /home/william/fornecedores_insumos.csv
# Robusto: staging table + WHERE NOT EXISTS (não depende de constraint unique em cnpj).
set -euo pipefail
CSV="${1:?uso: bash importar_insumos.sh <caminho_do_csv>}"
DB="wins_hub-db-1"
[ -f "$CSV" ] || { echo "CSV não encontrado: $CSV"; exit 1; }
echo "linhas no CSV: $(($(wc -l < "$CSV")-1))"

# 1) staging
sudo docker exec -i "$DB" psql -U postgres -d wins_hub -c "
DROP TABLE IF EXISTS _stg_insumos;
CREATE TABLE _stg_insumos (cnpj text, divisao_cnae text, cnae_principal text, uf text,
  municipio text, razao_social text, capital_social text);"

# 2) \copy via STDIN (CSV está no host)
cat "$CSV" | sudo docker exec -i "$DB" psql -U postgres -d wins_hub \
  -c "\copy _stg_insumos FROM STDIN WITH (FORMAT csv, HEADER true)"

# 3) higieniza + insere só os que ainda não existem
sudo docker exec -i "$DB" psql -U postgres -d wins_hub <<'SQL'
-- só dígitos no cnpj, capital numérico, descarta lixo
DELETE FROM _stg_insumos WHERE length(regexp_replace(coalesce(cnpj,''),'\D','','g'))<>14;
INSERT INTO fornecedores (cnpj, divisao_cnae, cnae_principal, uf, razao_social, capital_social, situacao_cadastral, status)
SELECT s.cnpj, s.divisao_cnae, s.cnae_principal, s.uf, s.razao_social,
       NULLIF(s.capital_social,'')::numeric, '02', 'importado_sc'
FROM _stg_insumos s
WHERE NOT EXISTS (SELECT 1 FROM fornecedores f WHERE f.cnpj = s.cnpj);
SELECT 'importados (status=importado_sc)' AS info, count(*) FROM fornecedores WHERE status='importado_sc';
SELECT divisao_cnae, count(*) FROM fornecedores WHERE status='importado_sc' GROUP BY 1 ORDER BY 1;
DROP TABLE _stg_insumos;
SQL
echo "=== concedendo grant já garantido; pronto. Rode materializar_cadeia_obra depois. ==="
