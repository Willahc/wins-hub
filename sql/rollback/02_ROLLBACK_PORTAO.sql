-- Rollback operacional do Portão (não apaga auditoria por padrão)
BEGIN;
UPDATE wins_v2.portao_config SET valor='false'
 WHERE chave IN (
   'PORTAO_OBRAS_ENABLED',
   'PORTAO_OBRAS_NEW_CAPTURES_ENABLED',
   'PORTAO_OBRAS_HISTORICAL_ENABLED',
   'PORTAO_OBRAS_AGENT_ENABLED',
   'AUTO_ENRICH_AFTER_GATE_ENABLED'
 );
-- Opcional: dropar triggers (mantém colunas/auditoria)
-- DROP TRIGGER IF EXISTS trg_portao_nova_captura ON public.obras;
-- DROP TRIGGER IF EXISTS trg_portao_enfileirar ON public.obras;
COMMIT;
