-- Snapshot + rollback tables for historical sanitization
BEGIN;

ALTER TABLE public.obras DROP CONSTRAINT IF EXISTS obras_status_portao_ck;
ALTER TABLE public.obras
  ADD CONSTRAINT obras_status_portao_ck CHECK (
    status_portao IS NULL OR status_portao = ANY (ARRAY[
      'EM_ANALISE'::text,
      'EM_ANALISE_MANUAL'::text,
      'APROVADA'::text,
      'REJEITADA'::text,
      'ERRO_PORTAO'::text
    ])
  );

CREATE TABLE IF NOT EXISTS wins_v2.portao_snapshot_pre_historico (
  snapshot_id bigserial PRIMARY KEY,
  obra_id uuid NOT NULL,
  status_portao text,
  visivel boolean,
  motivo_invisivel text,
  classificacao_computed text,
  status_enriquecimento text,
  fase_real_obra text,
  valor_estimado numeric,
  fonte text,
  setor text,
  empresa text,
  id_externo text,
  nome text,
  capturado_em timestamptz DEFAULT now()
);

TRUNCATE wins_v2.portao_snapshot_pre_historico;

INSERT INTO wins_v2.portao_snapshot_pre_historico (
  obra_id, status_portao, visivel, motivo_invisivel, classificacao_computed,
  status_enriquecimento, fase_real_obra, valor_estimado, fonte, setor,
  empresa, id_externo, nome
)
SELECT id, status_portao, visivel, motivo_invisivel, classificacao_computed,
       status_enriquecimento, fase_real_obra, valor_estimado, fonte, setor,
       empresa, id_externo, left(nome,500)
FROM public.obras;

CREATE TABLE IF NOT EXISTS wins_v2.portao_rollback_historico (
  id bigserial PRIMARY KEY,
  obra_id uuid NOT NULL,
  status_portao_anterior text,
  visivel_anterior boolean,
  classificacao_anterior text,
  motivo_invisivel_anterior text,
  status_enriquecimento_anterior text,
  motivo text,
  status_portao_novo text,
  visivel_novo boolean,
  lote text,
  timestamp timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portao_rollback_obra ON wins_v2.portao_rollback_historico(obra_id);
CREATE INDEX IF NOT EXISTS idx_portao_snapshot_obra ON wins_v2.portao_snapshot_pre_historico(obra_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON wins_v2.portao_snapshot_pre_historico TO wins_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON wins_v2.portao_rollback_historico TO wins_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wins_v2 TO wins_app;

UPDATE wins_v2.portao_config SET valor='true', atualizado_em=now()
WHERE chave='PORTAO_OBRAS_HISTORICAL_ENABLED';

INSERT INTO wins_v2.portao_config (chave, valor, nota) VALUES
  ('PORTAO_SANITIZACAO_LOTE', '0', 'contador de lotes da sanitizacao historica')
ON CONFLICT (chave) DO UPDATE SET valor='0', atualizado_em=now();

COMMIT;
