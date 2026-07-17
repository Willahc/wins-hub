-- =========================================================================
-- Migration: Camada 4 Stack Proprio - email_validacao_cache
-- Data: 2026-05-10
-- TTL curto (30 dias) - emails podem virar invalidos rapido
-- =========================================================================

CREATE TABLE IF NOT EXISTS email_validacao_cache (
    email                TEXT        PRIMARY KEY,
    status               VARCHAR(30) NOT NULL CHECK (status IN (
                            'pending','inferred_pattern','verified_mx',
                            'verified_smtp','invalid','bounce',
                            'catch_all','greylisted'
                         )),
    sintaxe_ok           BOOLEAN     NOT NULL DEFAULT FALSE,
    mx_record            TEXT,
    smtp_response_code   INTEGER,
    smtp_response_msg    TEXT,
    catch_all            BOOLEAN     NOT NULL DEFAULT FALSE,
    fonte_validacao      VARCHAR(40) NOT NULL,  -- 'sintaxe'|'mx'|'smtp'|'hunter:valid'|...
    confianca            VARCHAR(10) NOT NULL CHECK (confianca IN ('alta','media','baixa')),
    validated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proxima_revalidacao  DATE        DEFAULT (CURRENT_DATE + INTERVAL '30 days')
);

CREATE INDEX IF NOT EXISTS idx_email_val_cache_status
    ON email_validacao_cache (status);
CREATE INDEX IF NOT EXISTS idx_email_val_cache_revalidacao
    ON email_validacao_cache (proxima_revalidacao);
CREATE INDEX IF NOT EXISTS idx_email_val_cache_dominio
    ON email_validacao_cache ((split_part(email, '@', 2)));

COMMENT ON TABLE email_validacao_cache IS
'Camada 4 STACK PROPRIO. Cache de validacoes SMTP/Hunter por email. '
'TTL curto (30 dias) porque emails podem virar invalidos. status alinhado '
'com check de empresa_decisores_cache.email_status (8 valores).';
