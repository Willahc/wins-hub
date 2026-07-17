-- Patch C (bug #2): auditoria de plano != GRATUITO sem pagamento aprovado.
-- O endpoint de signup está LIMPO (força GRATUITO), então a fonte do plano
-- pago-sem-pagamento (caso Anderson) é OUTRO caminho: promoção admin, import,
-- ou pagamento de teste. Um trigger no nível da tabela captura QUALQUER fonte.
--
-- DRYRUN: roda tudo dentro de uma transação e faz ROLLBACK no fim — valida
-- sintaxe/efeito sem persistir. O par _COMMIT.sql é idêntico mas com COMMIT.
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

ROLLBACK;
