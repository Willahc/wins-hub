"""Backfill matchmaking — loopa sobre obras sem match e chama gerar_matches_para_obra.

Não respeita janela canônica (02-07 BRT) — é manual, chama o serviço direto.
Logga progresso em /tmp/matchmaking_backfill.log dentro do container.
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/app")

import psycopg2  # type: ignore
from services.matchmaking import gerar_matches_para_obra  # type: ignore

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)

LOG_PATH = "/tmp/matchmaking_backfill.log"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def main() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.id::text, o.fonte, o.uf, o.setor
                FROM obras o
                WHERE o.visivel = true
                  AND o.setor IS NOT NULL
                  AND o.uf IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM matches_obra_prestador m
                      WHERE m.obra_id = o.id
                  )
                ORDER BY o.criado_em DESC
                """
            )
            obras = cur.fetchall()
    finally:
        conn.close()

    total = len(obras)
    log(f"START backfill — {total} obras elegíveis sem match")

    inicio = time.time()
    matches_total = 0
    erros = 0
    for i, (obra_id, fonte, uf, setor) in enumerate(obras, 1):
        try:
            r = gerar_matches_para_obra(obra_id) or {}
            n = r.get("matches_gerados", 0) or 0
            matches_total += n
        except Exception as e:
            erros += 1
            if erros <= 5:
                log(f"  ERRO obra {obra_id}: {e}")

        if i % 10 == 0 or i == total:
            elapsed = time.time() - inicio
            rate = i / elapsed if elapsed else 0
            eta_s = (total - i) / rate if rate else 0
            log(
                f"PROGRESSO {i}/{total} ({i*100/total:.1f}%) "
                f"matches={matches_total} erros={erros} "
                f"rate={rate:.2f} obras/s eta={eta_s/3600:.1f}h"
            )

    elapsed = time.time() - inicio
    log(f"FIM — {total} obras processadas em {elapsed/60:.1f} min, "
        f"{matches_total} matches, {erros} erros")


if __name__ == "__main__":
    main()
