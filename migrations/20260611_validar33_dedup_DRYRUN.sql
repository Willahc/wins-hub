\timing on
BEGIN;
-- 1) DEDUP: marca a cópia sem url_fonte como invisível
UPDATE obras SET visivel=false,
   motivo_invisivel='duplicata_pncp_serpro_20260611',
   observacoes_validacao = COALESCE(observacoes_validacao,'') || ' | auto:dedup_v1 keep:bd849445'
 WHERE id::text LIKE '690b40f5%';
SELECT 'dedup_marcada_invisivel' m, COUNT(*) n FROM obras WHERE id::text LIKE '690b40f5%' AND NOT visivel;

-- 2) VALIDA + recompute o lote (após dedup → 32 visíveis)
CREATE TEMP TABLE _v ON COMMIT DROP AS
  SELECT id, nome, setor, uf, valor_estimado FROM obras o
  WHERE o.visivel=true AND o.setor IS NOT NULL AND o.setor<>'OUTRO' AND o.uf IS NOT NULL
    AND o.classificacao_computed IS NULL AND o.validacao_obra_at IS NULL
    AND o.valor_estimado >= 10e6 AND COALESCE(o.fonte_tipo,'OFICIAL') IN ('OFICIAL','MANUAL');
SELECT 'a_validar (esperado 32)' m, COUNT(*) n FROM _v;
UPDATE obras SET validacao_obra_at=NOW(),
   observacoes_validacao = COALESCE(observacoes_validacao,'') || ' | auto:validacao_lote_oficial_20260611'
 WHERE id IN (SELECT id FROM _v);
DO $$ DECLARE r record; BEGIN FOR r IN SELECT id FROM _v LOOP PERFORM recompute_classificacao_obra(r.id); END LOOP; END $$;

SELECT 'DISTRIBUICAO_FINAL' m, o.classificacao_computed tier, COUNT(*) n
FROM _v v JOIN obras o ON o.id=v.id GROUP BY o.classificacao_computed ORDER BY 3 DESC;
ROLLBACK;
