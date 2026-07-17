-- =========================================================================
-- Migration: Camada 3 Stack Próprio - empresa_decisores_cache
-- Data: 2026-05-10
-- Estratégia: busca ampla (48 termos PT+EN), storage canônico (11 Anderson)
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE IF NOT EXISTS empresa_decisores_cache (
    id              SERIAL PRIMARY KEY,
    cnpj            VARCHAR(14) NOT NULL,

    nome_pessoa     TEXT NOT NULL,

    cargo_raw       TEXT NOT NULL,
    cargo_normalizado TEXT,
    cargo_idioma    VARCHAR(5) CHECK (cargo_idioma IN ('pt-br','en') OR cargo_idioma IS NULL),
    cargo_nivel     VARCHAR(20) CHECK (cargo_nivel IN ('estrategico','tatico','operacional') OR cargo_nivel IS NULL),
    tipo_cargo      TEXT CHECK (tipo_cargo IN (
                        'GERENTE_SUPRIMENTOS',
                        'GERENTE_COMPRAS',
                        'SUPPLY_CHAIN',
                        'GERENTE_PROJETOS',
                        'GERENTE_ENGENHARIA',
                        'GERENTE_INDUSTRIAL',
                        'COORDENADOR_OBRAS',
                        'COORDENADOR_MANUTENCAO',
                        'ENGENHEIRO_MECANICO_CIVIL',
                        'PROJETISTA',
                        'OUTRO'
                    )),

    confianca       VARCHAR(10) NOT NULL CHECK (confianca IN ('alta','media','baixa')),
    fonte_descoberta VARCHAR(30) NOT NULL,
    fonte_secundaria VARCHAR(30),
    snippet_origem  TEXT,
    url_origem      TEXT,
    linkedin_slug   TEXT,

    email           TEXT,
    email_status    VARCHAR(30) CHECK (email_status IN ('pending','inferred_pattern','verified_mx','verified_smtp','invalid','bounce') OR email_status IS NULL),

    score_relevancia NUMERIC(3,2) DEFAULT 0.0 CHECK (score_relevancia >= 0.0 AND score_relevancia <= 1.0),

    descoberto_em   TIMESTAMPTZ DEFAULT NOW(),
    atualizado_em   TIMESTAMPTZ DEFAULT NOW(),
    revalidacao     DATE DEFAULT (CURRENT_DATE + INTERVAL '180 days'),
    ultimo_contato  TIMESTAMPTZ,
    excluido_em     TIMESTAMPTZ
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_decisores_cache_cnpj
    ON empresa_decisores_cache (cnpj) WHERE excluido_em IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisores_cache_tipo_cargo
    ON empresa_decisores_cache (tipo_cargo) WHERE excluido_em IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisores_cache_confianca
    ON empresa_decisores_cache (confianca) WHERE excluido_em IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisores_cache_revalidacao
    ON empresa_decisores_cache (revalidacao) WHERE excluido_em IS NULL;

CREATE INDEX IF NOT EXISTS idx_decisores_cache_linkedin
    ON empresa_decisores_cache (linkedin_slug) WHERE linkedin_slug IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_decisores_cache_email
    ON empresa_decisores_cache (email) WHERE email IS NOT NULL;

-- UNIQUE funcional pra evitar duplicatas semânticas (João vs JOÃO vs Joao)
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisores_cache_cnpj_pessoa_uniq
    ON empresa_decisores_cache (cnpj, lower(unaccent(nome_pessoa)))
    WHERE excluido_em IS NULL;

-- Trigger pra manter atualizado_em
CREATE OR REPLACE FUNCTION trg_empresa_decisores_cache_atualizado_em()
RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_empresa_decisores_cache_atualizado_em
    ON empresa_decisores_cache;

CREATE TRIGGER trg_empresa_decisores_cache_atualizado_em
    BEFORE UPDATE ON empresa_decisores_cache
    FOR EACH ROW
    EXECUTE FUNCTION trg_empresa_decisores_cache_atualizado_em();

COMMENT ON TABLE empresa_decisores_cache IS
'Camada 3 STACK PRÓPRIO. 1 CNPJ = N decisores. Busca ampla (48 termos PT+EN), storage canônico (11 valores tipo_cargo). Substitui Hunter pra descoberta. Cache 180 dias. Decisor médio fica 18-24 meses no mesmo cargo.';
