-- Migration: 2026-05-22 — Realocar CNAE 4292801 (obras portuárias) de
-- "Rodovias e Aeroportos" para "Petróleo, Gás e E&P".
--
-- Root cause: time-ideal /api/obras/{id}/time-ideal pra obras setor
-- PETROLEO_GAS retornava "Rodovias e Aeroportos" como categoria esperada
-- + GAP (falso negativo), porque CNAE 4292801 estava em ambos:
--   - setor_cnae_compatibility(setor_obra='PETROLEO_GAS', peso=0.8)
--   - categorias_servico ROD_AER 'Rodovias e Aeroportos' cnaes
-- 4292801 = "Construção de obras portuárias, marítimas e fluviais" —
-- claramente offshore/E&P, não rodovia/aeroporto. Mapping era simplesmente
-- errado em ROD_AER (legado).
--
-- Fix: tirar 4292801 de ROD_AER + adicionar em PETR_OGE. REFRESH matview.

BEGIN;

-- Remove de Rodovias e Aeroportos
UPDATE categorias_servico
SET cnaes = ARRAY(
  SELECT unnest(cnaes) EXCEPT SELECT '4292801'
)::text[]
WHERE codigo = 'ROD_AER';

-- Adiciona em PETR_OGE (idempotente: array_append não duplica se já existir
-- via filtro NOT contains)
UPDATE categorias_servico
SET cnaes = CASE
  WHEN '4292801' = ANY(cnaes) THEN cnaes
  ELSE cnaes || ARRAY['4292801']::text[]
END
WHERE codigo = 'PETR_OGE';

REFRESH MATERIALIZED VIEW cnaes_interesse;

COMMIT;

-- Verify
SELECT codigo, nome, cnaes
FROM categorias_servico
WHERE codigo IN ('ROD_AER', 'PETR_OGE')
ORDER BY codigo;
