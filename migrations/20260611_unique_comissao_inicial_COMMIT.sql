-- ============================================================
-- COMMIT (B4) — só rodar após autorização explícita ("COMMIT")
-- e dry-run limpo. Mesmo DDL do dry-run, sem os testes.
-- Tabela vazia (11/06) → CREATE INDEX normal é instantâneo,
-- sem necessidade de CONCURRENTLY.
-- ============================================================
\timing on
BEGIN;

CREATE UNIQUE INDEX uq_comissoes_inicial_por_prestador
    ON comissoes (prestador_id)
 WHERE tipo = 'INICIAL';

COMMIT;

-- B5 — validação pós-commit
SELECT indexname FROM pg_indexes
 WHERE tablename = 'comissoes'
   AND indexname = 'uq_comissoes_inicial_por_prestador';
