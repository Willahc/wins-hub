#!/usr/bin/env python3
"""
comissoes_diario.py
Cron diário 00:30 — duas tarefas idempotentes:

  A) Marcar comissões 'pendente' que passaram 30d como 'disponivel'
     (UPDATE simples, idempotente por SET status='disponivel' já idempotente)

  B) Criar comissões RECORRENTES para leads que assinaram entre 30d e 360d atrás
     - Mês N = assinou_em + 30*N dias (1..12), só cria se já passou
     - Skip se cliente voltou pra GRATUITO (cancelou) ou plano_expira < NOW()
     - Idempotência: NÃO INSERT se já existe (lead_outbound_id, RECORRENTE, mês)

Uso:
    docker exec wins_hub-api-1 python /app/scripts/comissoes_diario.py            # dry-run
    docker exec wins_hub-api-1 python /app/scripts/comissoes_diario.py --commit   # produção
"""
import sys
sys.path.insert(0, "/app")

import argparse
import os
import logging
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from services.comissoes import calcular_comissao_lead


DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "dbname": os.environ.get("DB_NAME", "wins_hub"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

# Logging em stderr pra não conflitar com prints de progresso
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("comissoes_diario")


def marcar_disponiveis(conn, dry_run: bool):
    """Tarefa A: pendente → disponivel quando disponivel_em <= NOW()."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM comissoes
            WHERE status='pendente' AND disponivel_em IS NOT NULL AND disponivel_em <= NOW()
        """)
        n = cur.fetchone()[0]
        log.info("Tarefa A: %d comissões pendentes prontas para virar 'disponivel'", n)
        if n == 0:
            return 0
        if dry_run:
            log.info("[dry-run] não atualizando")
            return n
        cur.execute("""
            UPDATE comissoes
            SET status='disponivel'
            WHERE status='pendente' AND disponivel_em IS NOT NULL AND disponivel_em <= NOW()
        """)
        return n


def criar_recorrentes(conn, dry_run: bool):
    """Tarefa B: cria comissões RECORRENTES devidas para cada mês 1..12."""
    criadas = 0
    pulou_cancelado = 0
    pulou_idempotencia = 0

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Pegar candidatos: leads que assinaram entre 30d e 360d atrás
        cur.execute("""
            SELECT lo.id AS lead_id,
                   lo.prestador_id,
                   lo.representante_id,
                   lo.assinou_em,
                   lo.valor_pago_centavos,
                   p.plano AS plano_atual,
                   p.plano_expira,
                   FLOOR(EXTRACT(EPOCH FROM (NOW() - lo.assinou_em)) / 86400)::int AS dias_desde_assinou
            FROM leads_outbound lo
            JOIN prestadores p ON p.id = lo.prestador_id
            WHERE lo.assinou_em IS NOT NULL
              AND lo.representante_id IS NOT NULL
              AND lo.valor_pago_centavos > 0
              AND lo.assinou_em <= NOW() - INTERVAL '30 days'
              AND lo.assinou_em >= NOW() - INTERVAL '360 days'
        """)
        candidatos = cur.fetchall()

    log.info("Tarefa B: %d leads candidatos a RECORRENTE", len(candidatos))

    for c in candidatos:
        # Filtro de cancelamento
        plano_atual = (c['plano_atual'] or 'GRATUITO').upper()
        plano_expira = c['plano_expira']
        cancelou = (plano_atual == 'GRATUITO') or (
            plano_expira and plano_expira < datetime.now(timezone.utc)
        )
        if cancelou:
            pulou_cancelado += 1
            log.info("  skip lead=%s prestador=%s — cliente voltou pra %s/expirou",
                     c['lead_id'], c['prestador_id'], plano_atual)
            continue

        # Quais meses já passaram (1..12)? Cada mês = 30 dias
        dias = c['dias_desde_assinou']
        max_mes = min(12, dias // 30)
        if max_mes < 1:
            continue

        for mes in range(1, max_mes + 1):
            # Idempotência: já existe?
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM comissoes
                    WHERE lead_outbound_id=%s AND tipo='RECORRENTE' AND recorrencia_mes=%s
                    LIMIT 1
                """, (c['lead_id'], mes))
                if cur.fetchone():
                    pulou_idempotencia += 1
                    continue

            if dry_run:
                log.info("  [dry-run] criaria RECORRENTE lead=%s mes=%s valor_base=%s",
                         c['lead_id'], mes, c['valor_pago_centavos'])
                criadas += 1
                continue

            try:
                cid = calcular_comissao_lead(
                    conn,
                    c['prestador_id'],
                    c['valor_pago_centavos'],
                    'RECORRENTE',
                    recorrencia_mes=mes,
                    lead_outbound_id_explicit=c['lead_id'],
                )
                if cid:
                    criadas += 1
                    log.info("  + RECORRENTE id=%s lead=%s mes=%s", cid, c['lead_id'], mes)
            except Exception as e:
                log.exception("  ERRO criando RECORRENTE lead=%s mes=%s: %s",
                              c['lead_id'], mes, e)

    return {
        "candidatos": len(candidatos),
        "criadas": criadas,
        "pulou_cancelado": pulou_cancelado,
        "pulou_idempotencia": pulou_idempotencia,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Executar de verdade (default: dry-run)")
    args = parser.parse_args()
    dry_run = not args.commit

    log.info("===== comissoes_diario %s =====", "(DRY-RUN)" if dry_run else "(COMMIT)")
    started = datetime.now(timezone.utc)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        n_disp = marcar_disponiveis(conn, dry_run)
        stats = criar_recorrentes(conn, dry_run)

        if dry_run:
            log.info("[dry-run] não commitando — rolling back")
            conn.rollback()
        else:
            conn.commit()
            log.info("✓ commit OK")

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        log.info(
            "RESUMO: pendente→disponivel=%d | recorrentes_candidatos=%d criadas=%d "
            "skip_cancelado=%d skip_idempotencia=%d | tempo=%.1fs",
            n_disp, stats["candidatos"], stats["criadas"],
            stats["pulou_cancelado"], stats["pulou_idempotencia"], elapsed,
        )
    except Exception as e:
        log.exception("FATAL: %s", e)
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
