-- ============================================================
-- Backfill decisores_obra ← obras.nivel1_* (V9 pipeline catchup)
-- Executado: 2026-05-14
-- Inseridas: 242 rows
-- ouro_count live: 292 → 532 (+240)
-- ============================================================

-- B1: função de mapeamento
CREATE OR REPLACE FUNCTION mapear_tipo_cargo(cargo_raw TEXT) RETURNS TEXT AS $$
DECLARE c TEXT;
BEGIN
  IF cargo_raw IS NULL OR TRIM(cargo_raw)='' THEN RETURN 'OUTRO'; END IF;
  c := lower(unaccent(cargo_raw));
  RETURN CASE
    WHEN c ~ '(suprimento|sourcing|aquisicao|procurement|comprador|buyer)' THEN 'GERENTE_SUPRIMENTOS'
    WHEN c ~ '(supply[- ]?chain|cadeia[- ]?suprimentos)' THEN 'SUPPLY_CHAIN'
    WHEN c ~ 'compras?' THEN 'GERENTE_COMPRAS'
    WHEN c ~ '(engenharia[- ]?mecanic|engenharia[- ]?civil|engenheiro[- ]?mecanic|engenheiro[- ]?civil|engenharia[- ]?eletric|mecanic|eletrotec|eletrici)' THEN 'ENGENHEIRO_MECANICO_CIVIL'
    WHEN c ~ '(engenharia|engineering|engenheiro)' THEN 'GERENTE_ENGENHARIA'
    WHEN c ~ '(projetista|drafter)' THEN 'PROJETISTA'
    WHEN c ~ '(projetos?|project)' THEN 'GERENTE_PROJETOS'
    WHEN c ~ '(manutencao|maintenance)' THEN 'COORDENADOR_MANUTENCAO'
    WHEN c ~ '(industrial|fabril|fabrica|planta\W|plant[- ]?manager)' THEN 'GERENTE_INDUSTRIAL'
    WHEN c ~ '(obras|construcao|construction)' THEN 'COORDENADOR_OBRAS'
    WHEN c ~ '(operacoes|operations)' THEN 'GERENTE_INDUSTRIAL'
    ELSE 'OUTRO'
  END;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- B3: backfill (242 rows inseridas)
INSERT INTO decisores_obra (
  obra_id, nome, cargo, tipo_cargo, email, linkedin_url, telefone, fonte, registrado_por, registrado_em
)
SELECT
  o.id, o.nivel1_nome,
  COALESCE(NULLIF(o.nivel1_cargo,''), 'Decisor'),
  mapear_tipo_cargo(o.nivel1_cargo),
  NULLIF(o.nivel1_email,''), NULLIF(o.nivel1_linkedin,''), NULLIF(o.nivel1_telefone,''),
  'BACKFILL_NIVEL1_V9', 'auto:backfill_nivel1_v9', NOW()
FROM obras o
WHERE o.classificacao_computed='OURO' AND o.visivel=true
  AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome <> ''
  AND (NULLIF(o.nivel1_email,'') IS NOT NULL OR NULLIF(o.nivel1_linkedin,'') IS NOT NULL)
  AND NOT EXISTS (
    SELECT 1 FROM decisores_obra d
    WHERE d.obra_id=o.id AND d.excluido_em IS NULL
  )
ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING;

-- B5 (parcial): reconciliar stored OURO conforme critério live
UPDATE obras SET classificacao_computed = CASE
  WHEN EXISTS (
    SELECT 1 FROM decisores_obra d
    WHERE d.obra_id=obras.id AND d.excluido_em IS NULL
      AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'
      AND ((COALESCE(d.email,'') <> '') OR (COALESCE(d.linkedin_url,'') <> ''))
  ) THEN 'OURO'
  WHEN (nivel1_nome IS NOT NULL AND nivel1_nome <> ''
    AND lower(unaccent(nivel1_cargo)) ~ '(compras|suprimentos|supply|procurement|sourcing|engenh|projetos|obras|manutenc|industrial)'
    AND COALESCE(fonte_tipo,'OFICIAL') <> 'NOTICIA') THEN 'PRATA'
  ELSE NULL
END
WHERE visivel=true AND classificacao_computed='OURO';
