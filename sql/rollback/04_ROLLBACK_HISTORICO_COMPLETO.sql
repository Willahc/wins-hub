-- Restaura estado pré-sanitização a partir do snapshot (TODAS as obras).
-- USAR SÓ EM EMERGÊNCIA. Não apaga auditoria portao_decisoes / portao_rollback_historico.
BEGIN;
UPDATE public.obras o
SET
  status_portao = s.status_portao,
  visivel = s.visivel,
  motivo_invisivel = s.motivo_invisivel,
  classificacao_computed = s.classificacao_computed,
  status_enriquecimento = s.status_enriquecimento,
  fase_real_obra = s.fase_real_obra
FROM wins_v2.portao_snapshot_pre_historico s
WHERE o.id = s.obra_id;

UPDATE wins_v2.portao_config SET valor='false', atualizado_em=now()
WHERE chave='PORTAO_OBRAS_HISTORICAL_ENABLED';
COMMIT;
