-- 01_funil.sql — Funil do portão sobre os últimos 30 dias.
-- Baseline versionado: detecta drift na taxa de entrada vs. critério.
WITH base AS (
  SELECT o.*,
    (cnpj ~ '^[0-9]{14}$' AND cnpj_status IN ('ok','validated_brasilapi','validated_manual_brasilapi')) AS c_cnpj,
    (setor IS NOT NULL AND setor <> 'OUTRO' AND setor IN (SELECT DISTINCT setor FROM setor_categorias)) AS c_setor,
    (valor_estimado >= 10000000 AND capex_fonte IS NULL) AS c_capex_real,
    (valor_estimado >= 10000000) AS c_capex_qualquer,
    (COALESCE(uf,'') <> '' AND COALESCE(municipio,'') <> '') AS c_local
  FROM obras o
  WHERE criado_em >= now() - interval '30 days'
)
SELECT
  count(*) AS total_30d,
  count(*) FILTER (WHERE c_cnpj)           AS pass_cnpj,
  count(*) FILTER (WHERE c_setor)          AS pass_setor,
  count(*) FILTER (WHERE c_capex_real)     AS pass_capex_real,
  count(*) FILTER (WHERE c_capex_qualquer) AS pass_capex_inc_estim,
  count(*) FILTER (WHERE c_local)          AS pass_local,
  count(*) FILTER (WHERE c_cnpj AND c_setor AND c_capex_real AND c_local)     AS pass_TODOS_literal,
  count(*) FILTER (WHERE c_cnpj AND c_setor AND c_capex_qualquer AND c_local) AS pass_TODOS_inc_estim
FROM base;
