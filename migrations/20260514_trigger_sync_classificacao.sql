-- ============================================================
-- Trigger sync_classificacao_after_decisor
-- Mantém obras.classificacao_computed sincronizada com decisores_obra.
-- Escopo: apenas OURO/PRATA (preserva PIPELINE/NULL antigos).
-- Executado: 2026-05-14
-- ============================================================

CREATE OR REPLACE FUNCTION recompute_classificacao_obra(p_obra_id UUID)
RETURNS VOID AS $$
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
    ELSE classificacao_computed
  END
  WHERE id = p_obra_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_sync_classificacao()
RETURNS TRIGGER AS $$
BEGIN
  PERFORM recompute_classificacao_obra(COALESCE(NEW.obra_id, OLD.obra_id));
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sync_classificacao_after_decisor ON decisores_obra;
CREATE TRIGGER sync_classificacao_after_decisor
AFTER INSERT OR UPDATE OR DELETE ON decisores_obra
FOR EACH ROW EXECUTE FUNCTION trg_sync_classificacao();
