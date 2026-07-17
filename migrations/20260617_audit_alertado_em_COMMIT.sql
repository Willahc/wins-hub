-- Leitor do audit table (Patch C, passo 2): coluna pra marcar linha já alertada.
-- Versão COMMIT. Idempotente (IF NOT EXISTS). Aplicar após revisar o DRYRUN.
BEGIN;

ALTER TABLE plano_alteracoes_suspeitas
    ADD COLUMN IF NOT EXISTS alertado_em timestamptz;

CREATE INDEX IF NOT EXISTS idx_plano_susp_nao_alertado
    ON plano_alteracoes_suspeitas (id) WHERE alertado_em IS NULL;

COMMIT;
