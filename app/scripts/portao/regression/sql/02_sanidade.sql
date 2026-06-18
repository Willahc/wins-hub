-- 02_sanidade.sql — Quanto do portão LITERAL (AND rígido) descartaria que hoje é útil.
-- Guarda de regressão: prova por que município/estimativa são SOFT, não rejeição.
WITH base AS (
  SELECT o.*,
    (cnpj ~ '^[0-9]{14}$' AND cnpj_status IN ('ok','validated_brasilapi','validated_manual_brasilapi')) AS c_cnpj,
    (setor IS NOT NULL AND setor <> 'OUTRO' AND setor IN (SELECT DISTINCT setor FROM setor_categorias)) AS c_setor,
    (valor_estimado >= 10000000 AND capex_fonte IS NULL) AS c_capex_real,
    (COALESCE(uf,'') <> '' AND COALESCE(municipio,'') <> '') AS c_local
  FROM obras o WHERE criado_em >= now() - interval '30 days'
)
SELECT 'passaria_literal' AS cenario, COALESCE(classificacao_computed,'(NULL)') c, count(*)
FROM base WHERE c_cnpj AND c_setor AND c_capex_real AND c_local GROUP BY 2
UNION ALL
SELECT 'DESCARTADO_mas_hoje_util', COALESCE(classificacao_computed,'(NULL)'), count(*)
FROM base WHERE NOT (c_cnpj AND c_setor AND c_capex_real AND c_local)
  AND classificacao_computed IN ('OURO','PRATA','BRONZE') GROUP BY 2
ORDER BY 1,3 DESC;
