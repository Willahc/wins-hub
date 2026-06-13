\timing on
BEGIN;

-- ============================================================
-- Expansao catalogo industrial — demanda matchmaking (13/06/2026)
-- Estende categorias_servico.cnaes (categorias JA mapeadas a setores)
-- para cobrir divisoes CNAE 25/33/42 ausentes do catalogo (vies-construcao).
-- Destrava prestadores industriais que estavam com 0 matches:
--   MetaInox (2521700), LPA Service (3314799/3321000), Singapura (4292802)
--   -> ~4.200-4.400 obras elegiveis cada (cap 500 armazenado).
-- ADITIVO PURO: nenhuma categoria nova, nenhum setor_categorias novo, nada removido.
-- Aplicado em prod 13/06; este arquivo registra a migration no repo.
-- ============================================================

WITH adds(codigo, novos) AS (VALUES
  ('MONT_IND', ARRAY['4292802','3321000']),            -- Obras de Montagem Industrial; Instalacao de Maquinas e Equip. Industriais
  ('MANU_IND', ARRAY['3314799','3314710','3314719']),  -- Manut/Rep de Maquinas (industriais / uso geral / alimentos-bebidas)
  ('CALD_SOL', ARRAY['2521700']))                      -- Fab. Tanques, Reservatorios Metalicos e Caldeiras
UPDATE categorias_servico c
SET cnaes = (SELECT array_agg(DISTINCT x ORDER BY x) FROM unnest(c.cnaes || a.novos) x)
FROM adds a
WHERE c.codigo = a.codigo
  AND NOT (c.cnaes @> a.novos);   -- idempotente: so atualiza se faltar algum

-- Conferencia pos-update
SELECT codigo, cnaes FROM categorias_servico
 WHERE codigo IN ('MONT_IND','MANU_IND','CALD_SOL') ORDER BY codigo;

COMMIT;

-- ------------------------------------------------------------
-- Rollback — remove so os CNAEs adicionados:
--   UPDATE categorias_servico SET cnaes = array_remove(array_remove(cnaes,'4292802'),'3321000') WHERE codigo='MONT_IND';
--   UPDATE categorias_servico SET cnaes = array_remove(array_remove(array_remove(cnaes,'3314799'),'3314710'),'3314719') WHERE codigo='MANU_IND';
--   UPDATE categorias_servico SET cnaes = array_remove(cnaes,'2521700') WHERE codigo='CALD_SOL';
-- ------------------------------------------------------------
