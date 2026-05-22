-- Migration: 2026-05-22 — Rename matches_obra_prestador → matches_legacy
--
-- Objetivo: consolidar matches_v2 como fonte única de matchmaking.
-- matches_obra_prestador (v1) tem 7.15M rows / 8013 obras / 96k CNPJs
-- com schema rico (categoria_id, ranking, nivel_proximidade, distancia_km,
-- escopo). matches_v2 tem 354k rows / 4336 obras / 19k CNPJs com schema
-- minimalista (score + breakdown).
--
-- Estratégia: rename para sinalizar deprecação + view backward-compat
-- pra não quebrar os 72 refs em código ativo (15 arquivos em wins-hub
-- e wins-hub-v2: alertas CNAE realtime/semanal, orchestrator, services,
-- dashboards, fornecedores, prestadores routes, frontend v2 index.html).
--
-- View é auto-updatable (single table, no joins/agg) — INSERT/UPDATE/DELETE
-- continuam funcionando transparentemente. Drop view + table quando
-- migração para matches_v2 completar (refs zeradas).

BEGIN;

ALTER TABLE matches_obra_prestador RENAME TO matches_legacy;

CREATE VIEW matches_obra_prestador AS SELECT * FROM matches_legacy;

COMMENT ON TABLE matches_legacy IS
  'Renomeada de matches_obra_prestador em 2026-05-22 (Wave matches_v2 consolidation). View matches_obra_prestador mantida pra backward compat — 72 refs em código ativo. Drop quando refs zeradas.';

COMMENT ON VIEW matches_obra_prestador IS
  'Backward-compat view sobre matches_legacy. Postgres auto-updatable (single table, no joins/agg). DROP VIEW quando todos os call sites migrarem pra matches_v2.';

COMMIT;
