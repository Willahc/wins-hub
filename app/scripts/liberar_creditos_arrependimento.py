#!/usr/bin/env python3
"""Libera créditos de prestadores que NÃO renunciaram ao direito de arrependimento (CDC art. 49)
após 8 dias do início do ciclo. Roda diariamente via cron.

Usage:
    python liberar_creditos_arrependimento.py            # dry-run
    python liberar_creditos_arrependimento.py --commit   # aplica UPDATE
"""
import argparse
import logging
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "wins_hub-db-1"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
    )


SELECT_PENDENTES = """
SELECT id, email, plano, ciclo_inicio,
       EXTRACT(EPOCH FROM (NOW() - ciclo_inicio))/86400.0 AS dias_decorridos
  FROM prestadores
 WHERE renunciou_arrependimento = FALSE
   AND creditos_liberados_em IS NULL
   AND ciclo_fim IS NOT NULL
   AND ciclo_inicio <= NOW() - INTERVAL '8 days'
   AND COALESCE(plano, 'GRATUITO') <> 'GRATUITO'
 ORDER BY ciclo_inicio
"""

UPDATE_LIBERAR = """
UPDATE prestadores
   SET creditos_liberados_em = NOW()
 WHERE renunciou_arrependimento = FALSE
   AND creditos_liberados_em IS NULL
   AND ciclo_fim IS NOT NULL
   AND ciclo_inicio <= NOW() - INTERVAL '8 days'
   AND COALESCE(plano, 'GRATUITO') <> 'GRATUITO'
RETURNING id
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="aplica UPDATE (default: dry-run)")
    args = parser.parse_args()

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SELECT_PENDENTES)
            pendentes = cur.fetchall()
        log.info("Pendentes de liberação D+8: %d", len(pendentes))
        for p in pendentes:
            log.info("  · %s (plano=%s, ciclo_inicio=%s, dias=%.1f)",
                     p["email"], p["plano"], p["ciclo_inicio"], p["dias_decorridos"])

        if not pendentes:
            return 0
        if not args.commit:
            log.info("DRY-RUN. Use --commit para aplicar.")
            return 0

        with conn.cursor() as cur:
            cur.execute(UPDATE_LIBERAR)
            liberados = cur.fetchall()
        conn.commit()
        log.info("Liberados: %d prestador(es)", len(liberados))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
