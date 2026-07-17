-- ============================================================
-- Matchmaker on-demand (V0.1.6) — controle de job
-- ============================================================
CREATE TABLE IF NOT EXISTS matchmaker_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  iniciado_por TEXT,
  iniciado_em TIMESTAMPTZ DEFAULT NOW(),
  finalizado_em TIMESTAMPTZ,
  status VARCHAR(20) DEFAULT 'RODANDO',
  obras_alvo INT,
  obras_processadas INT DEFAULT 0,
  matches_criados INT DEFAULT 0,
  pid INT,
  ultimo_obra_id UUID,
  erro TEXT
);

ALTER TABLE matchmaker_jobs DROP CONSTRAINT IF EXISTS matchmaker_status_check;
ALTER TABLE matchmaker_jobs ADD CONSTRAINT matchmaker_status_check
  CHECK (status IN ('RODANDO','PAUSADO','CONCLUIDO','ERRO'));

CREATE INDEX IF NOT EXISTS idx_matchmaker_jobs_status ON matchmaker_jobs(status);
CREATE INDEX IF NOT EXISTS idx_matchmaker_jobs_iniciado_em ON matchmaker_jobs(iniciado_em DESC);
