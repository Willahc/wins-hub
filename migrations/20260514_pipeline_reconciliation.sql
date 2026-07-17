-- ============================================================
-- PIPELINE Reconciliation V0.1.4 (Cenário C híbrido)
-- Executado: 2026-05-14
-- ============================================================

-- B1: Bucket A — Excluir 19.730 obras de fontes asset do PIPELINE
UPDATE obras SET classificacao_computed = NULL
WHERE classificacao_computed='PIPELINE' AND visivel=true
  AND fonte IN ('anm_cfem','mapa_sif','abiove_processadoras','unica_usinas');

-- B2: Bucket B — Fix bug antaq_tup (fase OPERACAO+outorga → LICENCA_INSTALACAO)
UPDATE obras SET fase='LICENCA_INSTALACAO'
WHERE fonte='antaq_tup' AND fase='OPERACAO' AND visivel=true
  AND descricao ILIKE '%outorga%';
-- Impacto: 551 obras

-- B3: PIPELINE_SQL afrouxado em main.py
-- Antes: fase IN (EM_EXECUCAO, PLANEJAMENTO, LICENCA_INSTALACAO, LICENCA_PREVIA), capex >= 100M
-- Depois: + LICITACAO_ABERTA + PROJETO, capex >= 10M

-- B4: recompute_classificacao_full(uuid) inclui PIPELINE
CREATE OR REPLACE FUNCTION recompute_classificacao_full(p_id UUID) RETURNS VOID AS $$
BEGIN
  UPDATE obras SET classificacao_computed = CASE
    WHEN COALESCE(fonte_tipo,'OFICIAL') = 'NOTICIA' THEN classificacao_computed
    WHEN EXISTS (
      SELECT 1 FROM decisores_obra d
      WHERE d.obra_id=obras.id AND d.excluido_em IS NULL
        AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'
        AND (NULLIF(d.email,'') IS NOT NULL OR NULLIF(d.linkedin_url,'') IS NOT NULL)
    ) THEN 'OURO'
    WHEN obras.nivel1_nome IS NOT NULL AND obras.nivel1_nome <> '' THEN 'PRATA'
    WHEN fonte IN ('anm_cfem','mapa_sif','abiove_processadoras','unica_usinas') THEN NULL
    WHEN fase IN ('EM_EXECUCAO','PLANEJAMENTO','LICENCA_INSTALACAO','LICENCA_PREVIA','LICITACAO_ABERTA','PROJETO')
      AND valor_estimado IS NOT NULL AND valor_estimado >= 10000000
      AND COALESCE(nivel1_nome,'') = ''
      AND COALESCE(fonte_tipo,'OFICIAL') <> 'NOTICIA'
    THEN 'PIPELINE'
    ELSE NULL
  END
  WHERE id = p_id;
END;
$$ LANGUAGE plpgsql;

-- Recompute em massa (trigger temporariamente OFF pra performance)
ALTER TABLE decisores_obra DISABLE TRIGGER sync_classificacao_after_decisor;
UPDATE obras SET classificacao_computed = CASE
  WHEN COALESCE(fonte_tipo,'OFICIAL') = 'NOTICIA' THEN classificacao_computed
  WHEN EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id=obras.id AND d.excluido_em IS NULL
      AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> 'OUTRO'
      AND (NULLIF(d.email,'') IS NOT NULL OR NULLIF(d.linkedin_url,'') IS NOT NULL)) THEN 'OURO'
  WHEN obras.nivel1_nome IS NOT NULL AND obras.nivel1_nome <> '' THEN 'PRATA'
  WHEN fonte IN ('anm_cfem','mapa_sif','abiove_processadoras','unica_usinas') THEN NULL
  WHEN fase IN ('EM_EXECUCAO','PLANEJAMENTO','LICENCA_INSTALACAO','LICENCA_PREVIA','LICITACAO_ABERTA','PROJETO')
    AND valor_estimado IS NOT NULL AND valor_estimado >= 10000000
    AND COALESCE(nivel1_nome,'') = ''
    AND COALESCE(fonte_tipo,'OFICIAL') <> 'NOTICIA' THEN 'PIPELINE'
  ELSE NULL
END
WHERE visivel=true;
ALTER TABLE decisores_obra ENABLE TRIGGER sync_classificacao_after_decisor;
