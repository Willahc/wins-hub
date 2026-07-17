-- SQL minimo do pipeline mestre V2 (schema wins_v2 existente).
-- Executar como postgres. Nao altera public.obras alem do trigger de inbox.
-- Nao publica obras V2. Nao mexe no Portao.

BEGIN;

-- ---------------------------------------------------------------------------
-- Colunas auxiliares em capturas_brutas
-- ---------------------------------------------------------------------------
ALTER TABLE wins_v2.capturas_brutas
    ADD COLUMN IF NOT EXISTS namespace text NOT NULL DEFAULT 'default';

ALTER TABLE wins_v2.capturas_brutas
    ADD COLUMN IF NOT EXISTS origem_marcador text NOT NULL DEFAULT 'CAPTURA_NOVA';

ALTER TABLE wins_v2.capturas_brutas
    ADD COLUMN IF NOT EXISTS v1_obra_id uuid;

ALTER TABLE wins_v2.capturas_brutas
    ADD COLUMN IF NOT EXISTS campos_canonicos jsonb;

ALTER TABLE wins_v2.capturas_brutas
    ADD COLUMN IF NOT EXISTS versao integer NOT NULL DEFAULT 1;

ALTER TABLE wins_v2.capturas_brutas
    DROP CONSTRAINT IF EXISTS capturas_brutas_origem_marcador_ck;

ALTER TABLE wins_v2.capturas_brutas
    ADD CONSTRAINT capturas_brutas_origem_marcador_ck
    CHECK (origem_marcador = ANY (ARRAY[
        'HISTORICO_IMPORTADO'::text,
        'CAPTURA_NOVA'::text,
        'REPROCESSAMENTO'::text
    ]));

-- Dedup oficial: fonte + namespace + id_externo + hash do payload
CREATE UNIQUE INDEX IF NOT EXISTS uq_capturas_brutas_dedup
    ON wins_v2.capturas_brutas (fonte_id, namespace, id_externo, hash_conteudo)
    WHERE id_externo IS NOT NULL AND hash_conteudo IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_capturas_brutas_v1_obra
    ON wins_v2.capturas_brutas (v1_obra_id)
    WHERE v1_obra_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_capturas_brutas_id_externo_ns
    ON wins_v2.capturas_brutas (fonte_id, namespace, id_externo);

-- ---------------------------------------------------------------------------
-- Fila de falhas / reprocessamento (sem timer)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wins_v2.pipeline_falhas (
    id              bigserial PRIMARY KEY,
    fonte           text,
    captador        text,
    id_externo      text,
    namespace       text NOT NULL DEFAULT 'default',
    payload         jsonb,
    contexto        jsonb,
    erro            text NOT NULL,
    status          text NOT NULL DEFAULT 'pendente',
    tentativas      integer NOT NULL DEFAULT 0,
    criado_em       timestamptz NOT NULL DEFAULT now(),
    atualizado_em   timestamptz NOT NULL DEFAULT now(),
    resolvido_em    timestamptz,
    CONSTRAINT pipeline_falhas_status_ck CHECK (
        status = ANY (ARRAY[
            'pendente'::text,
            'reprocessando'::text,
            'resolvido'::text,
            'desistido'::text
        ])
    )
);

CREATE INDEX IF NOT EXISTS idx_pipeline_falhas_status
    ON wins_v2.pipeline_falhas (status, criado_em);

-- Inbox leve apos INSERT em public.obras (capturas novas)
CREATE TABLE IF NOT EXISTS wins_v2.pipeline_inbox (
    id              bigserial PRIMARY KEY,
    v1_obra_id      uuid,
    fonte           text,
    id_externo      text,
    payload_minimo  jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'pendente',
    erro            text,
    criado_em       timestamptz NOT NULL DEFAULT now(),
    processado_em   timestamptz,
    CONSTRAINT pipeline_inbox_status_ck CHECK (
        status = ANY (ARRAY[
            'pendente'::text,
            'processado'::text,
            'ignorado'::text,
            'erro'::text
        ])
    )
);

CREATE INDEX IF NOT EXISTS idx_pipeline_inbox_status
    ON wins_v2.pipeline_inbox (status, criado_em);

-- ---------------------------------------------------------------------------
-- Upsert seguro de entidades_lookup (SECURITY DEFINER)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION wins_v2.upsert_entidade_lookup_minimo(
    p_cnpj              text,
    p_razao_social      text DEFAULT NULL,
    p_nome_fantasia     text DEFAULT NULL,
    p_natureza          text DEFAULT NULL,
    p_municipio         text DEFAULT NULL,
    p_uf                text DEFAULT NULL,
    p_fonte             text DEFAULT NULL,
    p_captador          text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = wins_v2, public
AS $$
DECLARE
    v_cnpj text := regexp_replace(COALESCE(p_cnpj, ''), '\D', '', 'g');
    v_id   uuid;
    v_uf   text;
BEGIN
    IF v_cnpj IS NULL OR length(v_cnpj) <> 14 THEN
        RETURN NULL;
    END IF;
    IF NOT wins_v2.cnpj_dv_valido(v_cnpj) THEN
        RETURN NULL;
    END IF;

    v_uf := NULLIF(upper(btrim(COALESCE(p_uf, ''))), '');
    IF v_uf IS NOT NULL AND v_uf !~ '^[A-Z]{2}$' THEN
        v_uf := NULL;
    END IF;

    INSERT INTO wins_v2.entidades_lookup (
        entidade_id,
        cnpj_normalizado,
        razao_social,
        nome_fantasia,
        natureza_entidade,
        municipio,
        uf,
        fontes,
        captadores,
        quantidade_ocorrencias,
        completude,
        confianca,
        necessita_enriquecimento_externo,
        importado_em
    ) VALUES (
        gen_random_uuid(),
        v_cnpj,
        NULLIF(btrim(COALESCE(p_razao_social, '')), ''),
        NULLIF(btrim(COALESCE(p_nome_fantasia, '')), ''),
        NULLIF(btrim(COALESCE(p_natureza, '')), ''),
        NULLIF(btrim(COALESCE(p_municipio, '')), ''),
        v_uf,
        NULLIF(btrim(COALESCE(p_fonte, '')), ''),
        NULLIF(btrim(COALESCE(p_captador, '')), ''),
        1,
        'pipeline',
        '0.80',
        'SIM',
        now()
    )
    ON CONFLICT (cnpj_normalizado) DO UPDATE SET
        razao_social = COALESCE(NULLIF(btrim(EXCLUDED.razao_social), ''), wins_v2.entidades_lookup.razao_social),
        nome_fantasia = COALESCE(NULLIF(btrim(EXCLUDED.nome_fantasia), ''), wins_v2.entidades_lookup.nome_fantasia),
        natureza_entidade = COALESCE(NULLIF(btrim(EXCLUDED.natureza_entidade), ''), wins_v2.entidades_lookup.natureza_entidade),
        municipio = COALESCE(NULLIF(btrim(EXCLUDED.municipio), ''), wins_v2.entidades_lookup.municipio),
        uf = COALESCE(EXCLUDED.uf, wins_v2.entidades_lookup.uf),
        fontes = CASE
            WHEN wins_v2.entidades_lookup.fontes IS NULL OR wins_v2.entidades_lookup.fontes = '' THEN EXCLUDED.fontes
            WHEN EXCLUDED.fontes IS NULL OR position(EXCLUDED.fontes in wins_v2.entidades_lookup.fontes) > 0
                THEN wins_v2.entidades_lookup.fontes
            ELSE wins_v2.entidades_lookup.fontes || '|' || EXCLUDED.fontes
        END,
        captadores = CASE
            WHEN wins_v2.entidades_lookup.captadores IS NULL OR wins_v2.entidades_lookup.captadores = '' THEN EXCLUDED.captadores
            WHEN EXCLUDED.captadores IS NULL OR position(EXCLUDED.captadores in wins_v2.entidades_lookup.captadores) > 0
                THEN wins_v2.entidades_lookup.captadores
            ELSE wins_v2.entidades_lookup.captadores || '|' || EXCLUDED.captadores
        END,
        quantidade_ocorrencias = COALESCE(wins_v2.entidades_lookup.quantidade_ocorrencias, 0) + 1,
        importado_em = now()
    RETURNING entidade_id INTO v_id;

    RETURN v_id;
END;
$$;

REVOKE ALL ON FUNCTION wins_v2.upsert_entidade_lookup_minimo(text, text, text, text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION wins_v2.upsert_entidade_lookup_minimo(text, text, text, text, text, text, text, text) TO wins_app;

-- ---------------------------------------------------------------------------
-- Trigger inbox (somente capturas novas em public.obras)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION wins_v2.trg_obras_pipeline_inbox()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = wins_v2, public
AS $$
BEGIN
    -- Nao processa aqui: apenas enfileira metadados minimos da V1.
    INSERT INTO wins_v2.pipeline_inbox (v1_obra_id, fonte, id_externo, payload_minimo, status)
    VALUES (
        NEW.id,
        NEW.fonte,
        NEW.id_externo,
        jsonb_build_object(
            'id', NEW.id,
            'id_externo', NEW.id_externo,
            'fonte', NEW.fonte,
            'nome', NEW.nome,
            'empresa', NEW.empresa,
            'cnpj', NEW.cnpj,
            'municipio', NEW.municipio,
            'uf', NEW.uf,
            'setor', NEW.setor,
            'valor_estimado', NEW.valor_estimado,
            'fase', NEW.fase,
            'url_fonte', NEW.url_fonte,
            'descricao', NEW.descricao,
            'data_anuncio', NEW.data_anuncio,
            'criado_em', NEW.criado_em
        ),
        'pendente'
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_obras_pipeline_inbox ON public.obras;
CREATE TRIGGER trg_obras_pipeline_inbox
    AFTER INSERT ON public.obras
    FOR EACH ROW
    WHEN (NEW.id_externo IS NOT NULL)
    EXECUTE FUNCTION wins_v2.trg_obras_pipeline_inbox();

-- ---------------------------------------------------------------------------
-- Permissoes minimas para wins_app no pipeline
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA wins_v2 TO wins_app;

GRANT SELECT, INSERT, UPDATE ON wins_v2.capturas_brutas TO wins_app;
GRANT SELECT, INSERT ON wins_v2.capturas_versoes TO wins_app;
GRANT SELECT, INSERT ON wins_v2.valores_normalizados TO wins_app;
GRANT SELECT, INSERT ON wins_v2.evidencias_campos TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.entidades TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.captura_entidades TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.fontes TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.captadores TO wins_app;
GRANT SELECT, INSERT ON wins_v2.campos_canonicos TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.pipeline_falhas TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.pipeline_inbox TO wins_app;
GRANT SELECT ON wins_v2.entidades_lookup TO wins_app;
GRANT INSERT ON wins_v2.enrichment_lookup_log TO wins_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wins_v2 TO wins_app;

COMMIT;
