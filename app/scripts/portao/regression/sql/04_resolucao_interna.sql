-- 04_resolucao_interna.sql — Fase 0: quanto resolve INTERNO (custo zero) vs. externo.
-- Usa idx_fornecedores_cnpj_raiz (lookup por raiz vira index scan, ~ms).
WITH obr AS (
  SELECT id, cnpj, substring(cnpj from 1 for 8) AS raiz
  FROM obras
  WHERE criado_em >= now() - interval '30 days'
    AND cnpj ~ '^[0-9]{14}$'
    AND cnpj_status IN ('ok','validated_brasilapi','validated_manual_brasilapi')
)
SELECT
  (SELECT count(*) FROM obr) AS obras_30d_cnpj_valido,
  (SELECT count(*) FROM obr o WHERE EXISTS (SELECT 1 FROM fornecedores f WHERE f.cnpj=o.cnpj))                            AS cnpj_full_fornecedores,
  (SELECT count(*) FROM obr o WHERE EXISTS (SELECT 1 FROM fornecedores f WHERE substring(f.cnpj from 1 for 8)=o.raiz))    AS cnpj_raiz_fornecedores,
  (SELECT count(*) FROM obr o WHERE EXISTS (SELECT 1 FROM empresa_dominios d
        WHERE (d.cnpj=o.cnpj OR substring(d.cnpj from 1 for 8)=o.raiz) AND COALESCE(d.dominio,'')<>''))                  AS dominio_interno,
  (SELECT count(*) FROM obr o WHERE EXISTS (SELECT 1 FROM decisores_preservados dp
        WHERE (dp.cnpj=o.cnpj OR substring(dp.cnpj from 1 for 8)=o.raiz) AND COALESCE(dp.email,'')<>''))                 AS decisor_interno_email;
