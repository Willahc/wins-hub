-- Rollback da reclassificação OURO regra final (restaura snapshot)
BEGIN;

UPDATE public.obras o
SET classificacao_computed = s.classificacao_anterior
FROM wins_v2.tier_ouro_regra_final_snapshot s
WHERE o.id = s.obra_id
  AND s.classificacao_anterior IS DISTINCT FROM o.classificacao_computed;

COMMIT;
