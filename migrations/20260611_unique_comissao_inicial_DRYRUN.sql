-- ============================================================
-- DRY-RUN (B2) — UNIQUE parcial em comissoes pra idempotência
-- de comissão INICIAL (webhook MP reentregue).
-- Contexto: auditoria 11/06/2026, fix #1c. INICIAL é única na
-- vida do prestador (services/comissoes.py); AVULSO/RECORRENTE
-- podem repetir e ficam fora do índice (UNIQUE parcial).
-- Executa tudo e ROLLBACK no fim — banco volta intacto.
-- ============================================================
\timing on
BEGIN;

-- SELECT before: duplicatas que bloqueariam o índice (esperado: 0 rows;
-- conferido em 11/06 — tabela comissoes está vazia)
SELECT prestador_id, COUNT(*) AS n
  FROM comissoes
 WHERE tipo = 'INICIAL'
 GROUP BY prestador_id
HAVING COUNT(*) > 1;

-- Mutação
CREATE UNIQUE INDEX uq_comissoes_inicial_por_prestador
    ON comissoes (prestador_id)
 WHERE tipo = 'INICIAL';

-- SELECT after: índice existe na transação
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE tablename = 'comissoes'
   AND indexname = 'uq_comissoes_inicial_por_prestador';

-- Sanidade: prova que o índice bloqueia a 2ª INICIAL do mesmo prestador.
-- (INSERTs sintéticos com prestador real qualquer; somem no ROLLBACK)
DO $$
DECLARE
    pid uuid;
BEGIN
    SELECT id INTO pid FROM prestadores LIMIT 1;
    IF pid IS NULL THEN
        RAISE NOTICE 'sem prestadores — teste pulado';
        RETURN;
    END IF;
    INSERT INTO comissoes (prestador_id, tipo, valor_base_centavos, pct_aplicado, valor_comissao_centavos)
    VALUES (pid, 'INICIAL', 10000, 50, 5000);
    BEGIN
        INSERT INTO comissoes (prestador_id, tipo, valor_base_centavos, pct_aplicado, valor_comissao_centavos)
        VALUES (pid, 'INICIAL', 10000, 50, 5000);
        RAISE EXCEPTION 'FALHA: segunda INICIAL passou — índice não bloqueou';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'OK: segunda INICIAL bloqueada por unique_violation (esperado)';
    END;
    -- AVULSO duplo deve passar (fora do índice parcial)
    INSERT INTO comissoes (prestador_id, tipo, valor_base_centavos, pct_aplicado, valor_comissao_centavos)
    VALUES (pid, 'AVULSO', 10000, 50, 5000),
           (pid, 'AVULSO', 10000, 50, 5000);
    RAISE NOTICE 'OK: AVULSO duplicado permitido (esperado)';
END $$;

ROLLBACK;
