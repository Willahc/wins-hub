-- Migration: 2026-05-22 — Registrar PETR_OGE e MINE_EXT em setor_categorias
--
-- Categorias criadas ontem (`2026-05-22_cnae_petroleo_mineracao.sql`) foram
-- inseridas em categorias_servico + setor_cnae_compatibility, mas NÃO em
-- setor_categorias (curadoria canônica setor → categorias-alvo).
--
-- Sem isso, o endpoint time-ideal — quando refatorado pra usar
-- setor_categorias como universo (em vez de scc.peso) — não incluiria
-- PETR_OGE em PETROLEO_GAS nem MINE_EXT em MINERACAO. Gap de curadoria.
--
-- Fix: 2 INSERTs idempotentes (ON CONFLICT DO NOTHING).

BEGIN;

INSERT INTO setor_categorias (setor, categoria_id, prioridade)
SELECT 'PETROLEO_GAS', cs.id, 5
FROM categorias_servico cs
WHERE cs.codigo = 'PETR_OGE'
ON CONFLICT (setor, categoria_id) DO NOTHING;

INSERT INTO setor_categorias (setor, categoria_id, prioridade)
SELECT 'MINERACAO', cs.id, 5
FROM categorias_servico cs
WHERE cs.codigo = 'MINE_EXT'
ON CONFLICT (setor, categoria_id) DO NOTHING;

COMMIT;

-- Verify
SELECT sc.setor, cs.codigo, cs.nome, sc.prioridade
FROM setor_categorias sc
JOIN categorias_servico cs ON cs.id = sc.categoria_id
WHERE cs.codigo IN ('PETR_OGE','MINE_EXT');
