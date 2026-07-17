BEGIN;
UPDATE public.obras o SET classificacao_computed = s.classificacao_snapshot
FROM wins_v2.ouro_quality_snapshot s
WHERE o.id = s.obra_id AND o.status_portao='APROVADA';
COMMIT;
