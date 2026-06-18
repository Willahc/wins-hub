-- 03_motivos.sql — Por qual critério as obras caem (motivo de descarte).
WITH base AS (
  SELECT o.*,
    (cnpj ~ '^[0-9]{14}$' AND cnpj_status IN ('ok','validated_brasilapi','validated_manual_brasilapi')) AS c_cnpj,
    (setor IS NOT NULL AND setor <> 'OUTRO' AND setor IN (SELECT DISTINCT setor FROM setor_categorias)) AS c_setor,
    (valor_estimado >= 10000000 AND capex_fonte IS NULL) AS c_capex_real,
    (COALESCE(uf,'') <> '' AND COALESCE(municipio,'') <> '') AS c_local
  FROM obras o WHERE criado_em >= now() - interval '30 days'
)
SELECT
  count(*) FILTER (WHERE NOT c_cnpj)       AS falha_cnpj,
  count(*) FILTER (WHERE NOT c_setor)      AS falha_setor,
  count(*) FILTER (WHERE NOT c_capex_real) AS falha_capex_real,
  count(*) FILTER (WHERE NOT c_local)      AS falha_local,
  count(*) FILTER (WHERE NOT c_local AND classificacao_computed IN ('OURO','PRATA')) AS falha_local_mas_ouro_prata,
  count(*) FILTER (WHERE NOT c_capex_real AND capex_fonte IS NOT NULL AND valor_estimado>=10000000) AS so_estimativa_senao_passava
FROM base;
