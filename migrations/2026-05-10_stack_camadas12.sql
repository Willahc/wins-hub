-- =========================================================================
-- Migration: stack proprio sales_intelligence - Camadas 1 e 2
-- Data: 2026-05-10
-- NAO APLICAR sem autorizacao explicita de William
-- =========================================================================

-- Camada 1: dossier completo da empresa
CREATE TABLE IF NOT EXISTS empresa_dossier_cache (
    cnpj                 VARCHAR(14) PRIMARY KEY,
    payload              JSONB       NOT NULL,
    coletado_em          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proxima_revalidacao  DATE
);

CREATE INDEX IF NOT EXISTS idx_dossier_cache_revalidacao
    ON empresa_dossier_cache (proxima_revalidacao);

CREATE INDEX IF NOT EXISTS idx_dossier_cache_dominio
    ON empresa_dossier_cache ((payload->'dominio_oficial'->>'dominio'));

COMMENT ON TABLE empresa_dossier_cache IS
    'Cache do dossier completo da empresa (Camada 1 sales_intelligence). '
    'Payload contem EmpresaDossier serializado. Default 30 dias de validade.';

-- Camada 2: padrao de email por dominio
CREATE TABLE IF NOT EXISTS empresa_email_pattern_cache (
    dominio              TEXT        PRIMARY KEY,
    padrao               TEXT        NOT NULL,
    confianca            VARCHAR(10) NOT NULL CHECK (confianca IN ('alta','media','baixa')),
    exemplos             JSONB       NOT NULL,
    amostra_total        INT         NOT NULL,
    detectado_em         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proxima_revalidacao  DATE
);

CREATE INDEX IF NOT EXISTS idx_pattern_cache_revalidacao
    ON empresa_email_pattern_cache (proxima_revalidacao);

CREATE INDEX IF NOT EXISTS idx_pattern_cache_confianca
    ON empresa_email_pattern_cache (confianca) WHERE confianca = 'alta';

COMMENT ON TABLE empresa_email_pattern_cache IS
    'Cache do padrao de email por dominio (Camada 2 sales_intelligence). '
    'Validade padrao 180 dias - padroes corporativos mudam menos que cadastrais.';
