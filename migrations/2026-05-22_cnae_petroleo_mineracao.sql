-- Migration: 2026-05-22 — CNAEs petróleo/gás + mineração em categorias_servico
--
-- Root cause: endpoint /api/obras/{id}/top-matches mapeia matches_v2.score_breakdown->>'cnae_codigo'
-- pra categoria via `cnae = ANY(cs.cnaes)`. Pra obras PETROLEO_GAS o engine v2 produz matches
-- com cnaes 0910600/1921700/0600001 (E&P, refino), nenhum mapeado → seção "Fornecedores
-- Compatíveis" vazia na UI mesmo com 47 matches válidos no DB.
--
-- Fix A: criar 2 categorias novas cobrindo upstream/midstream/downstream petróleo
-- + extração mineral. CNAEs deduplicados, codigo único exigido (NOT NULL+UNIQUE).

BEGIN;

INSERT INTO categorias_servico (codigo, nome, cnaes, ativo, ordem, essencial)
VALUES (
  'PETR_OGE',
  'Petróleo, Gás e E&P',
  ARRAY[
    '0600001',  -- Extração de petróleo e gás natural
    '0600002',  -- Extração e beneficiamento de xisto
    '0910600',  -- Atividades de apoio à extração de petróleo e gás
    '1921700',  -- Fabricação de produtos do refino de petróleo
    '1922501',  -- Formulação de combustíveis
    '1922502',  -- Rerrefino de óleos lubrificantes
    '3520401',  -- Produção de gás (overlap c/ Biogas — mas é categoria mais específica)
    '3520402',  -- Distribuição de combustíveis gasosos por redes urbanas
    '4681801',  -- Comércio atacadista de álcool carburante
    '4681802'   -- Comércio atacadista de combustíveis (gasolina/diesel/lubrif)
  ],
  true,
  410,  -- após PROC_OGS=400 ("Sistemas de Processo Oil Gas")
  true
)
ON CONFLICT (codigo) DO UPDATE
  SET cnaes = EXCLUDED.cnaes,
      ativo = EXCLUDED.ativo,
      ordem = EXCLUDED.ordem;

INSERT INTO categorias_servico (codigo, nome, cnaes, ativo, ordem, essencial)
VALUES (
  'MINE_EXT',
  'Mineração e Extração Mineral',
  ARRAY[
    '0710301',  -- Extração de minério de ferro
    '0710302',  -- Beneficiamento de minério de ferro
    '0721001',  -- Extração de minério de alumínio
    '0722801',  -- Extração de minério de estanho
    '0723301',  -- Extração de minério de manganês
    '0729401',  -- Extração de outros minerais metálicos não-ferrosos
    '0810001',  -- Extração de ardósia (variante de 0810099)
    '0810002',  -- Extração de granito
    '0891600',  -- Extração de minerais para fabricação de adubos e fertilizantes
    '0892401'   -- Extração de sal marinho
  ],
  true,
  265,  -- entre MINE_DRE=260 (Mineroduto) e MINE_BEN=270 ish
  true
)
ON CONFLICT (codigo) DO UPDATE
  SET cnaes = EXCLUDED.cnaes,
      ativo = EXCLUDED.ativo,
      ordem = EXCLUDED.ordem;

REFRESH MATERIALIZED VIEW cnaes_interesse;

COMMIT;

-- Verify
SELECT id, codigo, nome, array_length(cnaes,1) AS n_cnaes, ordem
FROM categorias_servico
WHERE codigo IN ('PETR_OGE','MINE_EXT');
