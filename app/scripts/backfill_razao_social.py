#!/usr/bin/env python3
"""Backfill razao_social via BrasilAPI pros CNPJs orfaos do dump RFB 2026-05-10.

Uso:
    python backfill_razao_social.py --batch 1000

Roda de hora em hora via cron `0 * * * *`. Auto-exit se 0 orfaos restantes.

BrasilAPI rate limit observado: ~300 req/min (free tier). Batch 1000 demora
~3-4min cada (rotacionar entre requests). Estimativa: 1.38M / (60K/dia × 24)
= ~23 dias rodando 24/7.

Schema: GET https://brasilapi.com.br/api/cnpj/v1/{cnpj} -> {razao_social: "..."}
"""
import argparse
import logging
import os
import sys
import time
from typing import Optional

import psycopg2
import requests

LOG_FMT = '%(asctime)s [%(levelname)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger('backfill')

DB = dict(
    host=os.environ.get('DB_HOST', 'localhost'),
    port=int(os.environ.get('DB_PORT', '5432')),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', 'WiNS@Hub2026!'),
    dbname=os.environ.get('DB_NAME', 'wins_hub'),
)

BRASILAPI_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
REQUEST_TIMEOUT = 8
SLEEP_BETWEEN = 0.25   # 240 req/min, conservador vs 300/min limit


def fetch_razao(cnpj: str) -> Optional[str]:
    """Fetch razao_social via BrasilAPI. Returns None se 404/erro."""
    try:
        r = requests.get(BRASILAPI_URL.format(cnpj=cnpj), timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            return (data.get('razao_social') or '').strip() or None
        if r.status_code == 429:
            log.warning("rate limit hit; sleep 60s")
            time.sleep(60)
        return None
    except requests.exceptions.RequestException as e:
        log.debug(f"err {cnpj}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch', type=int, default=1000,
                        help='Quantos CNPJs por execucao (default 1000)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    conn = psycopg2.connect(**DB)
    conn.autocommit = False

    # Sanity: count restante. Auto-exit se 0.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM fornecedores
            WHERE razao_social IS NULL OR TRIM(razao_social) = ''
        """)
        restante = cur.fetchone()[0]

    if restante == 0:
        log.info("0 fornecedores orfaos. Backfill completo. Desativar cron.")
        return 0

    log.info(f"orfaos restantes: {restante:,}")

    # Pega batch ordenado por porte (MEDIA/GRANDE primeiro = mais valor user-facing)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cnpj FROM fornecedores
            WHERE razao_social IS NULL OR TRIM(razao_social) = ''
            ORDER BY
              CASE porte_inferido
                WHEN 'GRANDE' THEN 1
                WHEN 'MEDIA' THEN 2
                WHEN 'PEQUENA' THEN 3
                ELSE 4
              END,
              cnpj
            LIMIT %s
        """, (args.batch,))
        cnpjs = [r[0] for r in cur.fetchall()]

    if not cnpjs:
        log.info("batch vazio (corrida com outra rodada?)")
        return 0

    log.info(f"processando batch de {len(cnpjs)}")
    sucessos = falhas = 0
    t0 = time.time()

    for i, cnpj in enumerate(cnpjs):
        razao = fetch_razao(cnpj)
        if razao:
            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE fornecedores SET razao_social=%s, atualizado_em=NOW() WHERE cnpj=%s",
                        (razao, cnpj)
                    )
                conn.commit()
            sucessos += 1
        else:
            falhas += 1

        time.sleep(SLEEP_BETWEEN)
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            log.info(f"  {i+1}/{len(cnpjs)} OK={sucessos} miss={falhas} ({rate:.1f} req/s)")

    conn.close()
    elapsed = time.time() - t0
    log.info(f"DONE batch={len(cnpjs)} sucesso={sucessos} falha={falhas} elapsed={elapsed:.0f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
