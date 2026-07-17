-- 2026-05-13: coluna escopo em matches_obra_prestador (regional|nacional)
--
-- Idempotente. Para o flow de matchmaking nacional (obras com setor mas sem UF),
-- usamos escopo='nacional' em vez do workaround nivel_proximidade='nacional'.
-- Migração one-shot: rows pré-existentes com nivel='nacional' viram escopo='nacional'
-- + nivel='distante' (sem cálculo geográfico significativo nesse caso).

ALTER TABLE matches_obra_prestador
    ADD COLUMN IF NOT EXISTS escopo VARCHAR(20) NOT NULL DEFAULT 'regional';

UPDATE matches_obra_prestador
   SET escopo='nacional', nivel_proximidade='distante'
 WHERE nivel_proximidade='nacional' AND escopo='regional';
