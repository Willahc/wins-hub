"""
alertar_plano_suspeito.py — Leitor do audit table (Patch C, passo 2).

Lê linhas novas de `plano_alteracoes_suspeitas` (plano != GRATUITO sem pagamento
aprovado, capturadas pela trigger `audita_plano_sem_pagamento`) e dispara um
alerta no Sentry pra cada uma. Idempotente: marca alertado_em=now(), então cada
linha é alertada exatamente 1×.

Fecha o loop do bug #2 (Anderson STANDARD sem pagamento): a trigger CAPTURA em
qualquer caminho (admin/import/código), este script ALERTA.

Uso:
  # Dry-run (mostra o que alertaria, NÃO envia ao Sentry, NÃO marca):
  docker exec wins_hub-api-1 python /app/scripts/alertar_plano_suspeito.py

  # Cron a cada 30min, efetiva:
  docker exec wins_hub-api-1 python /app/scripts/alertar_plano_suspeito.py --commit
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def _log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} [alertar_plano_suspeito] {msg}", flush=True)


def _init_sentry():
    """Mesmo padrão de main.py: no-op sem SENTRY_DSN."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return None
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, environment=os.getenv("SENTRY_ENV", "production"))
        return sentry_sdk
    except Exception as e:
        _log(f"Sentry init falhou: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Envia alertas ao Sentry e marca alertado_em. Sem isso = dry-run.")
    ap.add_argument("--limit", type=int, default=200,
                    help="Máx. de linhas por execução (default 200).")
    args = ap.parse_args()

    sentry = _init_sentry() if args.commit else None
    if args.commit and sentry is None:
        _log("ATENÇÃO: --commit sem SENTRY_DSN válido — vou marcar as linhas mas SEM enviar ao Sentry.")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, prestador_id, plano_antigo, plano_novo, detectado_em, contexto
                     FROM plano_alteracoes_suspeitas
                    WHERE alertado_em IS NULL
                    ORDER BY id
                    LIMIT %s""",
                (args.limit,),
            )
            rows = cur.fetchall()

        if not rows:
            _log("Nenhuma alteração de plano suspeita pendente. Nada a fazer.")
            return

        _log(f"{len(rows)} linha(s) pendente(s){' (DRY-RUN)' if not args.commit else ''}.")
        ids = []
        for r in rows:
            msg = (f"Plano suspeito: prestador {r['prestador_id']} "
                   f"{r['plano_antigo']}->{r['plano_novo']} sem pagamento aprovado")
            _log(f"  id={r['id']} {msg} (contexto={r['contexto']}, detectado={r['detectado_em']})")
            if args.commit and sentry is not None:
                with sentry.new_scope() as scope:
                    scope.set_tag("alerta", "plano_sem_pagamento")
                    scope.set_extra("prestador_id", str(r["prestador_id"]))
                    scope.set_extra("plano_antigo", r["plano_antigo"])
                    scope.set_extra("plano_novo", r["plano_novo"])
                    scope.set_extra("contexto", r["contexto"])
                    scope.set_extra("detectado_em", str(r["detectado_em"]))
                    sentry.capture_message(msg, level="warning")
            ids.append(r["id"])

        if args.commit:
            with conn.cursor() as cur2:
                cur2.execute(
                    "UPDATE plano_alteracoes_suspeitas SET alertado_em=now() WHERE id = ANY(%s)",
                    (ids,),
                )
            conn.commit()
            if sentry is not None:
                sentry.flush(timeout=10)
            _log(f"{len(ids)} linha(s) alertada(s) e marcada(s).")
        else:
            _log("DRY-RUN: nada enviado/marcado. Rode com --commit pra efetivar.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
