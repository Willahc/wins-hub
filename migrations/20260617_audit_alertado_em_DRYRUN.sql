-- Leitor do audit table (Patch C, passo 2): coluna pra marcar linha já alertada.
-- alertar_plano_suspeito.py seleciona WHERE alertado_em IS NULL, alerta no Sentry
-- e seta alertado_em=now() — idempotente (cada linha alertada 1×).
-- DRYRUN: BEGIN/ROLLBACK, não persiste.
BEGIN;

ALTER TABLE plano_alteracoes_suspeitas
    ADD COLUMN IF NOT EXISTS alertado_em timestamptz;

-- Index parcial: o leitor varre só as não-alertadas (tabela tende a ficar pequena,
-- mas mantém a query barata mesmo se crescer).
CREATE INDEX IF NOT EXISTS idx_plano_susp_nao_alertado
    ON plano_alteracoes_suspeitas (id) WHERE alertado_em IS NULL;

ROLLBACK;
