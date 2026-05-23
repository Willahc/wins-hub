-- Migration: 2026-05-23 — Expansão SCC para ENERGIA, MINERACAO, INFRAESTRUTURA
--
-- Antes:
--   ENERGIA: 14 CNAEs
--   MINERACAO: 6 CNAEs
--   INFRAESTRUTURA: 10 CNAEs
--
-- Padrão herdado de 2026-05-22_scc_petroleo_expansao.sql:
--   - Pesa via categorias_servico (CALD_SOL etc) → CNAEs
--   - ON CONFLICT GREATEST: nunca rebaixa peso existente
--   - REFRESH MATERIALIZED VIEW cnaes_interesse no fim
--
-- Critério de escolha: categorias claramente relevantes pra cada setor.
-- Conservador (5-6 categorias por setor) pra evitar diluir matchmaker.
-- "Mega Brief anterior" da sessão prévia não estava acessível na retomada —
-- escolhas baseadas em nomes de categorias_servico.

BEGIN;

-- ============== ENERGIA ==============
WITH cat_pesos(codigo, peso) AS (VALUES
    ('ENER_EOL', 0.85::numeric),  -- Energia Eolica EPC (parques eólicos)
    ('ENER_SOL', 0.85),  -- Energia Solar EPC (UFV/usinas solares)
    ('LINH_TRA', 0.80),  -- Linha de Transmissao
    ('SUBE_ALT', 0.85),  -- Subestacao e Alta Tensao
    ('MONT_ELM', 0.70),  -- Montagem Eletromecanica
    ('BIOG_MET', 0.55),  -- Biogas e Biometano
    ('BIOM_GER', 0.55),  -- Geracao a Biomassa
    ('INST_ELE', 0.65)   -- Instalacoes Eletricas
),
expandido AS (
  SELECT unnest(cs.cnaes) AS cnae_codigo, cp.peso
  FROM cat_pesos cp JOIN categorias_servico cs ON cs.codigo = cp.codigo
),
dedup AS (
  SELECT cnae_codigo, MAX(peso) AS peso FROM expandido GROUP BY cnae_codigo
)
INSERT INTO setor_cnae_compatibility (setor_obra, cnae_codigo, peso, fases_aplicaveis)
SELECT 'ENERGIA', cnae_codigo, peso,
       ARRAY['PLANEJAMENTO','LICITACAO_ABER','LICENCA_INSTALACAO','EM_EXECUCAO','OPERACAO']
FROM dedup
ON CONFLICT (setor_obra, cnae_codigo) DO UPDATE
  SET peso = GREATEST(setor_cnae_compatibility.peso, EXCLUDED.peso);

-- ============== MINERACAO ==============
WITH cat_pesos(codigo, peso) AS (VALUES
    ('MINE_EXT', 0.95::numeric),  -- Mineração e Extração Mineral (core)
    ('MINE_DRE', 0.85),  -- Mineroduto e Drenagem de Mina
    ('BRIT_BEN', 0.85),  -- Britagem e Beneficiamento
    ('PERF_DET', 0.80),  -- Perfuracao e Detonacao
    ('TRAN_MIN', 0.75),  -- Transporte de Minerio
    ('EMPI_FIL', 0.70),  -- Empilhamento e Filtragem
    ('DESC_BAR', 0.65),  -- Descaracterizacao de Barragens
    ('SOND_GEO', 0.65)   -- Sondagem e Geotecnia
),
expandido AS (
  SELECT unnest(cs.cnaes) AS cnae_codigo, cp.peso
  FROM cat_pesos cp JOIN categorias_servico cs ON cs.codigo = cp.codigo
),
dedup AS (
  SELECT cnae_codigo, MAX(peso) AS peso FROM expandido GROUP BY cnae_codigo
)
INSERT INTO setor_cnae_compatibility (setor_obra, cnae_codigo, peso, fases_aplicaveis)
SELECT 'MINERACAO', cnae_codigo, peso,
       ARRAY['PLANEJAMENTO','LICITACAO_ABER','LICENCA_INSTALACAO','EM_EXECUCAO','OPERACAO']
FROM dedup
ON CONFLICT (setor_obra, cnae_codigo) DO UPDATE
  SET peso = GREATEST(setor_cnae_compatibility.peso, EXCLUDED.peso);

-- ============== INFRAESTRUTURA ==============
WITH cat_pesos(codigo, peso) AS (VALUES
    ('ROD_AER', 0.85::numeric),  -- Rodovias e Aeroportos
    ('PAVIM_VIA', 0.80),  -- Pavimentacao e Vias
    ('OBRAS_ART', 0.85),  -- Obras de Arte Especiais (pontes, viadutos)
    ('FERR_VIA', 0.80),  -- Via Permanente Ferroviaria
    ('TERRA_MOV', 0.75),  -- Terraplanagem
    ('FUND_EST', 0.70),  -- Fundacao e Estacas
    ('ESTRU_CON', 0.65),  -- Estrutura de Concreto
    ('OBR_CIV', 0.55)    -- Construcao Civil
),
expandido AS (
  SELECT unnest(cs.cnaes) AS cnae_codigo, cp.peso
  FROM cat_pesos cp JOIN categorias_servico cs ON cs.codigo = cp.codigo
),
dedup AS (
  SELECT cnae_codigo, MAX(peso) AS peso FROM expandido GROUP BY cnae_codigo
)
INSERT INTO setor_cnae_compatibility (setor_obra, cnae_codigo, peso, fases_aplicaveis)
SELECT 'INFRAESTRUTURA', cnae_codigo, peso,
       ARRAY['PLANEJAMENTO','LICITACAO_ABER','LICENCA_INSTALACAO','EM_EXECUCAO']
FROM dedup
ON CONFLICT (setor_obra, cnae_codigo) DO UPDATE
  SET peso = GREATEST(setor_cnae_compatibility.peso, EXCLUDED.peso);

REFRESH MATERIALIZED VIEW cnaes_interesse;

COMMIT;

-- Verify
SELECT setor_obra, COUNT(*) AS cnaes_total, ROUND(AVG(peso),2) AS peso_medio
FROM setor_cnae_compatibility
WHERE setor_obra IN ('ENERGIA','MINERACAO','INFRAESTRUTURA')
GROUP BY setor_obra ORDER BY setor_obra;
