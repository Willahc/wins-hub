-- Cadastro Mestre de CNPJ. Executar como owner do schema wins_v2.
-- O script e idempotente e falha ao validar se houver CNPJ invalido legado.
BEGIN;

CREATE SCHEMA IF NOT EXISTS wins_v2;

CREATE OR REPLACE FUNCTION wins_v2.cnpj_dv_valido(value TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
DECLARE
    total INTEGER;
    digit_one INTEGER;
    digit_two INTEGER;
    position INTEGER;
BEGIN
    IF value !~ '^[0-9]{14}$' OR value = repeat(substr(value, 1, 1), 14) THEN
        RETURN FALSE;
    END IF;

    total := 0;
    FOR position IN 1..12 LOOP
        total := total
            + substr(value, position, 1)::INTEGER
            * CASE WHEN position <= 4 THEN 6 - position ELSE 14 - position END;
    END LOOP;
    digit_one := 11 - (total % 11);
    IF digit_one >= 10 THEN
        digit_one := 0;
    END IF;
    IF digit_one <> substr(value, 13, 1)::INTEGER THEN
        RETURN FALSE;
    END IF;

    total := 0;
    FOR position IN 1..13 LOOP
        total := total
            + substr(value, position, 1)::INTEGER
            * CASE WHEN position <= 5 THEN 7 - position ELSE 15 - position END;
    END LOOP;
    digit_two := 11 - (total % 11);
    IF digit_two >= 10 THEN
        digit_two := 0;
    END IF;

    RETURN digit_two = substr(value, 14, 1)::INTEGER;
END;
$function$;

-- Nao exponha funcoes do schema operacional pela permissao padrao de PUBLIC.
REVOKE ALL PRIVILEGES ON FUNCTION wins_v2.cnpj_dv_valido(TEXT) FROM PUBLIC;

CREATE TABLE IF NOT EXISTS wins_v2.entidades_lookup (
    entidade_id UUID PRIMARY KEY,
    cnpj_normalizado VARCHAR(14) NOT NULL,
    razao_social TEXT,
    nome_fantasia TEXT,
    natureza_entidade TEXT,
    situacao TEXT,
    endereco TEXT,
    municipio TEXT,
    uf VARCHAR(2),
    completude VARCHAR(50),
    confianca VARCHAR(50),
    fontes TEXT,
    captadores TEXT,
    primeira_observacao TIMESTAMPTZ,
    ultima_observacao TIMESTAMPTZ,
    campos_ausentes TEXT,
    importado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    versao_base VARCHAR(100),
    hash_origem VARCHAR(64),
    quantidade_ocorrencias INTEGER,
    necessita_enriquecimento_externo TEXT
);

-- As duas colunas abaixo sao as unicas adicoes ao layout legado de 19 colunas.
ALTER TABLE wins_v2.entidades_lookup
    ADD COLUMN IF NOT EXISTS quantidade_ocorrencias INTEGER,
    ADD COLUMN IF NOT EXISTS necessita_enriquecimento_externo TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_entidades_lookup_cnpj
    ON wins_v2.entidades_lookup (cnpj_normalizado);

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'wins_v2.entidades_lookup'::regclass
          AND conname = 'entidades_lookup_cnpj_dv_ck'
    ) THEN
        ALTER TABLE wins_v2.entidades_lookup
            ADD CONSTRAINT entidades_lookup_cnpj_dv_ck
            CHECK (wins_v2.cnpj_dv_valido(cnpj_normalizado)) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'wins_v2.entidades_lookup'::regclass
          AND conname = 'entidades_lookup_quantidade_ck'
    ) THEN
        ALTER TABLE wins_v2.entidades_lookup
            ADD CONSTRAINT entidades_lookup_quantidade_ck
            CHECK (quantidade_ocorrencias IS NULL OR quantidade_ocorrencias >= 0)
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'wins_v2.entidades_lookup'::regclass
          AND conname = 'entidades_lookup_uf_ck'
    ) THEN
        ALTER TABLE wins_v2.entidades_lookup
            ADD CONSTRAINT entidades_lookup_uf_ck
            CHECK (uf IS NULL OR uf ~ '^[A-Z]{2}$') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'wins_v2.entidades_lookup'::regclass
          AND conname = 'entidades_lookup_enriquecimento_ck'
    ) THEN
        ALTER TABLE wins_v2.entidades_lookup
            ADD CONSTRAINT entidades_lookup_enriquecimento_ck
            CHECK (
                necessita_enriquecimento_externo IS NULL
                OR upper(btrim(necessita_enriquecimento_externo)) IN ('SIM', 'NAO')
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'wins_v2.entidades_lookup'::regclass
          AND conname = 'entidades_lookup_hash_origem_ck'
    ) THEN
        ALTER TABLE wins_v2.entidades_lookup
            ADD CONSTRAINT entidades_lookup_hash_origem_ck
            CHECK (
                hash_origem IS NULL
                OR hash_origem ~ '^[0-9a-f]{64}$'
            ) NOT VALID;
    END IF;
END;
$migration$;

ALTER TABLE wins_v2.entidades_lookup
    VALIDATE CONSTRAINT entidades_lookup_cnpj_dv_ck;
ALTER TABLE wins_v2.entidades_lookup
    VALIDATE CONSTRAINT entidades_lookup_quantidade_ck;
ALTER TABLE wins_v2.entidades_lookup
    VALIDATE CONSTRAINT entidades_lookup_uf_ck;
ALTER TABLE wins_v2.entidades_lookup
    VALIDATE CONSTRAINT entidades_lookup_enriquecimento_ck;
ALTER TABLE wins_v2.entidades_lookup
    VALIDATE CONSTRAINT entidades_lookup_hash_origem_ck;

COMMENT ON TABLE wins_v2.entidades_lookup IS
    'Cadastro Mestre interno, uma entidade por CNPJ valido normalizado.';
COMMENT ON COLUMN wins_v2.entidades_lookup.versao_base IS
    'SHA-256 do conteudo canonico das 18 colunas da linha importada.';
COMMENT ON COLUMN wins_v2.entidades_lookup.hash_origem IS
    'SHA-256 da Planilha Mestre oficial validada antes da importacao.';

COMMIT;
