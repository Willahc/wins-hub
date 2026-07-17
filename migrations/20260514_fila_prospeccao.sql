-- ============================================================
-- Fila de Prospecção (V0.1.5)
-- 100 leads/rep/dia, pré-enriquecidos com BrasilAPI + Serper + HEAD HTTP
-- ============================================================

CREATE TABLE IF NOT EXISTS fila_prospeccao (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fornecedor_cnpj TEXT NOT NULL,
  obra_id UUID,
  setor TEXT,
  score_match NUMERIC,

  cnpj_ativo BOOLEAN,
  razao_social TEXT,
  site_url TEXT,
  site_ativo BOOLEAN,
  linkedin_url TEXT,
  email_generico TEXT,
  status_digital VARCHAR(20),
  enriquecido_em TIMESTAMPTZ,

  rep_atribuido TEXT,
  atribuido_em TIMESTAMPTZ,
  lote INT,

  status VARCHAR(20) DEFAULT 'PENDENTE',
  contatado_em TIMESTAMPTZ,
  resultado VARCHAR(30),
  observacoes TEXT,

  criado_em TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(fornecedor_cnpj)
);

CREATE INDEX IF NOT EXISTS idx_fila_rep_status ON fila_prospeccao(rep_atribuido, status);
CREATE INDEX IF NOT EXISTS idx_fila_lote ON fila_prospeccao(lote);
CREATE INDEX IF NOT EXISTS idx_fila_status_digital ON fila_prospeccao(status_digital);

ALTER TABLE fila_prospeccao DROP CONSTRAINT IF EXISTS fila_status_digital_check;
ALTER TABLE fila_prospeccao ADD CONSTRAINT fila_status_digital_check
  CHECK (status_digital IS NULL OR status_digital IN ('ATIVO','SEM_SITE','SEM_LINKEDIN','INVALIDO','INATIVO','PENDENTE'));

ALTER TABLE fila_prospeccao DROP CONSTRAINT IF EXISTS fila_status_check;
ALTER TABLE fila_prospeccao ADD CONSTRAINT fila_status_check
  CHECK (status IN ('PENDENTE','EM_CONTATO','RESPONDEU','NAO_ATENDE','INVALIDO','CONVERTIDO','PULADO'));
