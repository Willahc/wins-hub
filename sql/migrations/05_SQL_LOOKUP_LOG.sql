-- Auditoria de decisoes do resolvedor. Executar como owner de wins_v2.
BEGIN;

CREATE TABLE IF NOT EXISTS wins_v2.enrichment_lookup_log (
    id BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id UUID NOT NULL DEFAULT gen_random_uuid(),
    cnpj_normalizado VARCHAR(14),
    contexto TEXT,
    origem_da_solicitacao TEXT,
    status_lookup VARCHAR(50) NOT NULL,
    campos_solicitados TEXT,
    campos_encontrados TEXT,
    campos_ausentes TEXT,
    provedor_externo VARCHAR(100),
    chamada_externa_executada BOOLEAN NOT NULL DEFAULT FALSE,
    chamada_externa_evitada BOOLEAN NOT NULL DEFAULT FALSE,
    motivo TEXT,
    custo_estimado_evitado NUMERIC(10, 4) NOT NULL DEFAULT 0,
    tempo_lookup_ms NUMERIC(12, 3) NOT NULL DEFAULT 0,
    erro TEXT
);

ALTER TABLE wins_v2.enrichment_lookup_log
    ADD COLUMN IF NOT EXISTS request_id UUID;

-- gen_random_uuid() e VOLATILE: cada linha legada recebe UUID proprio.
UPDATE wins_v2.enrichment_lookup_log
   SET request_id = gen_random_uuid()
 WHERE request_id IS NULL;

ALTER TABLE wins_v2.enrichment_lookup_log
    ALTER COLUMN request_id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN request_id SET NOT NULL;

-- Recriar constraints conhecidas permite migrar definicoes antigas e repetir o SQL.
ALTER TABLE wins_v2.enrichment_lookup_log
    DROP CONSTRAINT IF EXISTS enrichment_lookup_log_status_ck,
    DROP CONSTRAINT IF EXISTS enrichment_lookup_log_external_flags_ck,
    DROP CONSTRAINT IF EXISTS enrichment_lookup_log_invalid_external_ck,
    DROP CONSTRAINT IF EXISTS enrichment_lookup_log_terminal_external_ck,
    DROP CONSTRAINT IF EXISTS enrichment_lookup_log_tempo_ck,
    DROP CONSTRAINT IF EXISTS enrichment_lookup_log_cnpj_formato_ck;

ALTER TABLE wins_v2.enrichment_lookup_log
    ADD CONSTRAINT enrichment_lookup_log_status_ck CHECK (
        status_lookup IN (
            'FULL_HIT', 'PARTIAL_HIT', 'MISS', 'INVALID',
            'SEM_CNPJ', 'CPF_NAO_APLICAVEL'
        )
    ) NOT VALID,
    ADD CONSTRAINT enrichment_lookup_log_external_flags_ck CHECK (
        NOT (chamada_externa_executada AND chamada_externa_evitada)
    ) NOT VALID,
    ADD CONSTRAINT enrichment_lookup_log_terminal_external_ck CHECK (
        status_lookup NOT IN ('INVALID', 'SEM_CNPJ', 'CPF_NAO_APLICAVEL')
        OR (
            NOT chamada_externa_executada
            AND NOT chamada_externa_evitada
        )
    ) NOT VALID,
    ADD CONSTRAINT enrichment_lookup_log_tempo_ck CHECK (
        tempo_lookup_ms >= 0
    ) NOT VALID,
    ADD CONSTRAINT enrichment_lookup_log_cnpj_formato_ck CHECK (
        cnpj_normalizado IS NULL
        OR cnpj_normalizado ~ '^[0-9]{14}$'
    ) NOT VALID;

ALTER TABLE wins_v2.enrichment_lookup_log
    VALIDATE CONSTRAINT enrichment_lookup_log_status_ck;
ALTER TABLE wins_v2.enrichment_lookup_log
    VALIDATE CONSTRAINT enrichment_lookup_log_external_flags_ck;
ALTER TABLE wins_v2.enrichment_lookup_log
    VALIDATE CONSTRAINT enrichment_lookup_log_terminal_external_ck;
ALTER TABLE wins_v2.enrichment_lookup_log
    VALIDATE CONSTRAINT enrichment_lookup_log_tempo_ck;
ALTER TABLE wins_v2.enrichment_lookup_log
    VALIDATE CONSTRAINT enrichment_lookup_log_cnpj_formato_ck;

DROP INDEX IF EXISTS wins_v2.idx_lookup_log_request_id;
CREATE INDEX idx_lookup_log_request_id
    ON wins_v2.enrichment_lookup_log (request_id);
CREATE INDEX IF NOT EXISTS idx_lookup_log_cnpj_timestamp
    ON wins_v2.enrichment_lookup_log (cnpj_normalizado, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_lookup_log_status_timestamp
    ON wins_v2.enrichment_lookup_log (status_lookup, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_lookup_log_contexto_timestamp
    ON wins_v2.enrichment_lookup_log (contexto, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_lookup_log_external_timestamp
    ON wins_v2.enrichment_lookup_log (
        chamada_externa_executada,
        chamada_externa_evitada,
        "timestamp" DESC
    );

COMMENT ON COLUMN wins_v2.enrichment_lookup_log.request_id IS
    'UUID obrigatorio que correlaciona decisao inicial e eventos de provedores.';

COMMIT;
