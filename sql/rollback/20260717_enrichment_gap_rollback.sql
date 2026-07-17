BEGIN;
UPDATE public.obras o
SET classificacao_computed = s.tier
FROM wins_v2.enrichment_gap_snapshot s
WHERE o.id = s.obra_id AND o.status_portao = 'APROVADA'
  AND s.tier IS NOT NULL AND s.tier IS DISTINCT FROM o.classificacao_computed;
COMMIT;
