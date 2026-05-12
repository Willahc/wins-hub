#!/bin/bash
set -e

WORK=/root/wins_hub/cnpj_etl
DATE_PASTA=2026-04-12
BASE_URL="https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos/${DATE_PASTA}"

mkdir -p $WORK
cd $WORK

log() { echo "[$(date '+%H:%M:%S')] $*"; }

sql() {
    docker exec -i wins_hub-db-1 sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1'
}

log "Validando conexao Postgres..."
echo "SELECT 1;" | sql > /dev/null
log "Ok."

log "Criando tabela temp..."
sql <<'SQL'
DROP TABLE IF EXISTS _etl_empresas_temp;
CREATE TABLE _etl_empresas_temp (
    cnpj_basico TEXT,
    razao_social TEXT,
    natureza_juridica TEXT,
    qualificacao_responsavel TEXT,
    capital_social TEXT,
    porte TEXT,
    ente_federativo TEXT
);
SQL

for i in 0 1 2 3 4 5 6 7 8 9; do
    ARQ="Empresas${i}.zip"
    log ">>> Empresas${i}"

    log "Baixando..."
    wget -q --show-progress -O "$ARQ" "${BASE_URL}/${ARQ}"

    log "Descompactando..."
    unzip -qo "$ARQ"
    rm -f "$ARQ"

    CSV=$(ls -1 | grep -i EMPRECSV | head -1)
    if [ -z "$CSV" ]; then log "ERRO: CSV nao achado"; ls -la; exit 1; fi
    log "CSV: $CSV ($(du -h "$CSV" | cut -f1))"

    log "Limpando temp..."
    echo "TRUNCATE _etl_empresas_temp;" | sql

    log "Copiando CSV pro container..."
    docker cp "$CSV" wins_hub-db-1:/tmp/emp.csv
    rm -f "$CSV"

    log "COPY pro Postgres..."
    sql <<'SQL'
COPY _etl_empresas_temp FROM '/tmp/emp.csv' WITH (FORMAT csv, DELIMITER ';', ENCODING 'LATIN1', QUOTE '"', HEADER FALSE);
SQL
    docker exec wins_hub-db-1 rm -f /tmp/emp.csv

    log "UPDATE join..."
    sql <<'SQL'
CREATE INDEX IF NOT EXISTS idx_etl_temp_cnpj_basico ON _etl_empresas_temp(cnpj_basico);

UPDATE empresas_receita er
SET razao_social = COALESCE(er.razao_social, NULLIF(t.razao_social, '')),
    porte = COALESCE(er.porte,
        CASE t.porte
            WHEN '01' THEN 'ME'
            WHEN '03' THEN 'EPP'
            WHEN '05' THEN 'DEMAIS'
            ELSE NULL
        END),
    capital_social = COALESCE(er.capital_social,
        NULLIF(REPLACE(t.capital_social, ',', '.'), '')::numeric),
    atualizado_em = NOW()
FROM _etl_empresas_temp t
WHERE LEFT(er.cnpj, 8) = t.cnpj_basico
  AND (er.razao_social IS NULL OR er.porte IS NULL OR er.capital_social IS NULL);
SQL
done

log "Drop tabela temp..."
echo "DROP TABLE _etl_empresas_temp;" | sql

log "===== STATS FINAIS ====="
sql <<'SQL'
SELECT
    COUNT(*) total,
    COUNT(razao_social) com_razao,
    ROUND(COUNT(razao_social)::numeric * 100 / COUNT(*), 1) pct_razao,
    COUNT(porte) com_porte,
    ROUND(COUNT(porte)::numeric * 100 / COUNT(*), 1) pct_porte
FROM empresas_receita;

SELECT
  COUNT(*) total_top100,
  COUNT(e.razao_social) com_razao_top100
FROM empresas_receita e
INNER JOIN (
  SELECT cnpj, COUNT(*) qtd FROM matches_obra_prestador GROUP BY cnpj ORDER BY qtd DESC LIMIT 100
) m ON e.cnpj = m.cnpj;
SQL

cd /root/wins_hub
rm -rf $WORK
log "FIM!"
