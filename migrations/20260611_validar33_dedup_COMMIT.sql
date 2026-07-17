\timing on
BEGIN;
-- 1) DEDUP
UPDATE obras SET visivel=false,
   motivo_invisivel='duplicata_pncp_serpro_20260611',
   observacoes_validacao = COALESCE(observacoes_validacao,'') || ' | auto:dedup_v1 keep:bd849445'
 WHERE id::text LIKE '690b40f5%';
-- 2) VALIDA o lote (exclui 2 borderline: 83cb13a9 transporte-serviço, 07f597d9 "Obras comuns")
UPDATE obras o SET validacao_obra_at=NOW(),
   observacoes_validacao = COALESCE(o.observacoes_validacao,'') || ' | auto:validacao_lote_oficial_20260611'
 WHERE o.visivel=true AND o.setor IS NOT NULL AND o.setor<>'OUTRO' AND o.uf IS NOT NULL
   AND o.classificacao_computed IS NULL AND o.validacao_obra_at IS NULL
   AND o.valor_estimado >= 10e6 AND COALESCE(o.fonte_tipo,'OFICIAL') IN ('OFICIAL','MANUAL')
   AND o.id::text NOT LIKE '83cb13a9%' AND o.id::text NOT LIKE '07f597d9%';
-- 3) RECOMPUTE individual
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT id FROM obras WHERE observacoes_validacao LIKE '%auto:validacao_lote_oficial_20260611%' LOOP
    PERFORM recompute_classificacao_obra(r.id); END LOOP;
END $$;
COMMIT;
