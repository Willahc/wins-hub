-- Patch C (bug #2): auditoria de plano != GRATUITO sem pagamento aprovado.
-- Versão COMMIT. Aplicar SOMENTE após revisar o DRYRUN. Idempotente
-- (CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE / DROP TRIGGER IF EXISTS).
--
-- Próximo passo (fora desta migration): um leitor (cron ou endpoint admin) que
-- lê plano_alteracoes_suspeitas e dispara sentry_sdk.capture_message nas novas
-- linhas — fecha o loop de alerta. A migration só garante a CAPTURA.
BEGIN;

CREATE TABLE IF NOT EXISTS plano_alteracoes_suspeitas (
    id            bigserial PRIMARY KEY,
    prestador_id  uuid        NOT NULL,
    plano_antigo  text,
    plano_novo    text,
    detectado_em  timestamptz NOT NULL DEFAULT now(),
    contexto      text
);

CREATE OR REPLACE FUNCTION trg_audita_plano_sem_pagamento() RETURNS trigger AS $$
BEGIN
    IF COALESCE(NEW.plano, 'GRATUITO') <> 'GRATUITO'
       AND NEW.plano IS DISTINCT FROM OLD.plano
       AND NOT EXISTS (
           SELECT 1 FROM pagamentos p
           WHERE p.prestador_id = NEW.id
             AND p.tipo = 'plano'
             AND p.status_local = 'aprovado'
       )
    THEN
        INSERT INTO plano_alteracoes_suspeitas (prestador_id, plano_antigo, plano_novo, contexto)
        VALUES (NEW.id, OLD.plano, NEW.plano, TG_OP || ' em prestadores.plano sem pagamento aprovado');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audita_plano_sem_pagamento ON prestadores;
CREATE TRIGGER audita_plano_sem_pagamento
    AFTER INSERT OR UPDATE OF plano ON prestadores
    FOR EACH ROW EXECUTE FUNCTION trg_audita_plano_sem_pagamento();

COMMIT;
