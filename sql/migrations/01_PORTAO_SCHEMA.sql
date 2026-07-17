-- =============================================================================
-- Portão de Obras — schema e auditoria (FASE 1)
-- Não publica wins_v2.obras_validadas. Não reclassifica histórico em massa.
-- =============================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- Colunas semânticas em public.obras (idempotente)
-- ---------------------------------------------------------------------------
ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS status_portao text;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS status_enriquecimento text;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS fase_real_obra text;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS portao_confianca numeric(5,4);

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS portao_motivo text;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS portao_versao text;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS portao_decidido_em timestamptz;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS portao_criterios jsonb;

ALTER TABLE public.obras
    ADD COLUMN IF NOT EXISTS portao_evidencias jsonb;

-- Defaults semânticos (não força histórico)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'obras_status_portao_ck'
    ) THEN
        ALTER TABLE public.obras
            ADD CONSTRAINT obras_status_portao_ck
            CHECK (
                status_portao IS NULL
                OR status_portao = ANY (ARRAY[
                    'EM_ANALISE'::text,
                    'APROVADA'::text,
                    'REJEITADA'::text,
                    'ERRO_PORTAO'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'obras_status_enriquecimento_ck'
    ) THEN
        ALTER TABLE public.obras
            ADD CONSTRAINT obras_status_enriquecimento_ck
            CHECK (
                status_enriquecimento IS NULL
                OR status_enriquecimento = ANY (ARRAY[
                    'NAO_INICIADO'::text,
                    'EM_PROCESSAMENTO'::text,
                    'COMPLETO'::text,
                    'PARCIAL'::text,
                    'INSUFICIENTE'::text,
                    'FALHA'::text
                ])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'obras_fase_real_obra_ck'
    ) THEN
        ALTER TABLE public.obras
            ADD CONSTRAINT obras_fase_real_obra_ck
            CHECK (
                fase_real_obra IS NULL
                OR fase_real_obra = ANY (ARRAY[
                    'PLANEJAMENTO'::text,
                    'LICENCIAMENTO'::text,
                    'LICITACAO'::text,
                    'CONTRATACAO'::text,
                    'EM_EXECUCAO'::text,
                    'PARALISADA'::text,
                    'CONCLUIDA'::text,
                    'DESCONHECIDA'::text
                ])
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_obras_status_portao
    ON public.obras (status_portao)
    WHERE status_portao IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_obras_portao_visivel
    ON public.obras (status_portao, visivel)
    WHERE status_portao = 'APROVADA';

-- ---------------------------------------------------------------------------
-- Config / feature flags (lidas por trigger e serviços)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wins_v2.portao_config (
    chave       text PRIMARY KEY,
    valor       text NOT NULL,
    atualizado_em timestamptz NOT NULL DEFAULT now(),
    nota        text
);

INSERT INTO wins_v2.portao_config (chave, valor, nota) VALUES
    ('PORTAO_OBRAS_ENABLED', 'false', 'Master switch do Portão'),
    ('PORTAO_OBRAS_NEW_CAPTURES_ENABLED', 'false', 'Protege novas capturas (invisível até decisão)'),
    ('PORTAO_OBRAS_HISTORICAL_ENABLED', 'false', 'Aplicar Portão no histórico (lotes)'),
    ('PORTAO_OBRAS_AGENT_ENABLED', 'false', 'Agente de análise EM_ANALISE'),
    ('AUTO_ENRICH_AFTER_GATE_ENABLED', 'false', 'Enriquece automaticamente após APROVADA'),
    ('PORTAO_VERSAO', 'portao-v5.0.0', 'Versão da matriz de regras')
ON CONFLICT (chave) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Fila operacional do Portão
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wins_v2.portao_fila (
    id              bigserial PRIMARY KEY,
    obra_id         uuid NOT NULL REFERENCES public.obras(id) ON DELETE CASCADE,
    captura_id      uuid,
    status          text NOT NULL DEFAULT 'pendente',
    tentativas      integer NOT NULL DEFAULT 0,
    max_tentativas  integer NOT NULL DEFAULT 5,
    proxima_tentativa timestamptz NOT NULL DEFAULT now(),
    ultimo_erro     text,
    criado_em       timestamptz NOT NULL DEFAULT now(),
    atualizado_em   timestamptz NOT NULL DEFAULT now(),
    processado_em   timestamptz,
    CONSTRAINT portao_fila_status_ck CHECK (
        status = ANY (ARRAY[
            'pendente'::text,
            'processando'::text,
            'concluido'::text,
            'erro'::text,
            'desistido'::text
        ])
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_portao_fila_obra_pendente
    ON wins_v2.portao_fila (obra_id)
    WHERE status IN ('pendente', 'processando');

CREATE INDEX IF NOT EXISTS idx_portao_fila_pendente
    ON wins_v2.portao_fila (status, proxima_tentativa)
    WHERE status = 'pendente';

-- ---------------------------------------------------------------------------
-- Auditoria append-only
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wins_v2.portao_decisoes (
    id                  bigserial PRIMARY KEY,
    obra_id             uuid NOT NULL,
    captura_id          uuid,
    status_anterior     text,
    status_novo         text NOT NULL,
    regra_aplicada      text,
    versao_portao       text NOT NULL,
    confianca           numeric(5,4),
    motivo              text NOT NULL,
    criterios_atendidos jsonb,
    criterios_ausentes  jsonb,
    evidencias          jsonb,
    campos_analisados   jsonb,
    origem_decisao      text NOT NULL DEFAULT 'regra',
    usuario_ou_agente   text NOT NULL DEFAULT 'portao_automatico',
    reverso             boolean NOT NULL DEFAULT false,
    reverte_decisao_id  bigint,
    criado_em           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_portao_decisoes_obra
    ON wins_v2.portao_decisoes (obra_id, criado_em DESC);

CREATE INDEX IF NOT EXISTS idx_portao_decisoes_status
    ON wins_v2.portao_decisoes (status_novo, criado_em DESC);

-- ---------------------------------------------------------------------------
-- Helper: ler flag
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION wins_v2.portao_flag(p_chave text, p_default text DEFAULT 'false')
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(
        (SELECT valor FROM wins_v2.portao_config WHERE chave = p_chave),
        p_default
    );
$$;

CREATE OR REPLACE FUNCTION wins_v2.portao_flag_on(p_chave text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT lower(wins_v2.portao_flag(p_chave, 'false'))
           IN ('1', 'true', 'yes', 'on', 'sim');
$$;

-- ---------------------------------------------------------------------------
-- Trigger: novas capturas sob Portão (só se flags ativas)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_portao_nova_captura()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, wins_v2
AS $$
DECLARE
    v_enabled boolean;
    v_new_cap boolean;
BEGIN
    BEGIN
        v_enabled := wins_v2.portao_flag_on('PORTAO_OBRAS_ENABLED');
        v_new_cap := wins_v2.portao_flag_on('PORTAO_OBRAS_NEW_CAPTURES_ENABLED');
    EXCEPTION WHEN OTHERS THEN
        RETURN NEW;
    END;

    IF NOT (v_enabled AND v_new_cap) THEN
        RETURN NEW;
    END IF;

    -- Não sobrescrever decisão manual/existente
    IF NEW.status_portao IS NOT NULL THEN
        RETURN NEW;
    END IF;

    NEW.status_portao := 'EM_ANALISE';
    NEW.status_enriquecimento := COALESCE(NEW.status_enriquecimento, 'NAO_INICIADO');
    NEW.visivel := false;
    NEW.motivo_invisivel := COALESCE(NULLIF(NEW.motivo_invisivel, ''), 'aguardando_portao');
    NEW.portao_versao := wins_v2.portao_flag('PORTAO_VERSAO', 'portao-v5.0.0');
    NEW.portao_motivo := COALESCE(NEW.portao_motivo, 'nova_captura_aguardando_portao');

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_portao_nova_captura ON public.obras;
CREATE TRIGGER trg_portao_nova_captura
    BEFORE INSERT ON public.obras
    FOR EACH ROW
    EXECUTE FUNCTION public.fn_portao_nova_captura();

-- Enfileira após insert
CREATE OR REPLACE FUNCTION public.fn_portao_enfileirar()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, wins_v2
AS $$
BEGIN
    IF NEW.status_portao = 'EM_ANALISE'
       AND wins_v2.portao_flag_on('PORTAO_OBRAS_ENABLED')
       AND wins_v2.portao_flag_on('PORTAO_OBRAS_NEW_CAPTURES_ENABLED')
    THEN
        IF NOT EXISTS (
            SELECT 1 FROM wins_v2.portao_fila
             WHERE obra_id = NEW.id
               AND status IN ('pendente', 'processando')
        ) THEN
            INSERT INTO wins_v2.portao_fila (obra_id, captura_id, status)
            VALUES (NEW.id, NEW.id, 'pendente');
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_portao_enfileirar ON public.obras;
CREATE TRIGGER trg_portao_enfileirar
    AFTER INSERT ON public.obras
    FOR EACH ROW
    EXECUTE FUNCTION public.fn_portao_enfileirar();

-- Enriquecimento: só após APROVADA quando Portão de novas capturas ativo
CREATE OR REPLACE FUNCTION public.fn_enqueue_enrichment()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  -- Se Portão ativo para novas capturas, só enfileira se APROVADA
  IF wins_v2.portao_flag_on('PORTAO_OBRAS_ENABLED')
     AND wins_v2.portao_flag_on('PORTAO_OBRAS_NEW_CAPTURES_ENABLED')
  THEN
    IF NEW.status_portao IS DISTINCT FROM 'APROVADA' THEN
      RETURN NEW;
    END IF;
    IF NOT wins_v2.portao_flag_on('AUTO_ENRICH_AFTER_GATE_ENABLED') THEN
      RETURN NEW;
    END IF;
  END IF;

  IF COALESCE(NEW.fonte,'') NOT IN ('anm_cfem','ibama_sislic')
     AND NEW.motivo_invisivel IS NULL
  THEN
    INSERT INTO enrichment_queue (obra_id, capex)
    VALUES (NEW.id, COALESCE(NEW.valor_estimado, 0))
    ON CONFLICT (obra_id) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$function$;

-- Permissões
GRANT SELECT, INSERT, UPDATE ON wins_v2.portao_config TO wins_app;
GRANT SELECT, INSERT, UPDATE ON wins_v2.portao_fila TO wins_app;
GRANT SELECT, INSERT ON wins_v2.portao_decisoes TO wins_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wins_v2 TO wins_app;
GRANT EXECUTE ON FUNCTION wins_v2.portao_flag(text, text) TO wins_app;
GRANT EXECUTE ON FUNCTION wins_v2.portao_flag_on(text) TO wins_app;

COMMIT;
