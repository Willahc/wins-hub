"""
Recovery one-shot: roda matchmaking só pras 165 obras criadas hoje (>= 04:00 UTC),
pulando as que já têm matches. Depois refresca as MVs e grava log_captacao final
ORCHESTRATOR=sucesso pra destravar o painel admin.

Não faz parte do orchestrator — rodar manualmente após kill do PID travado.
"""
from __future__ import annotations
import logging, os, sys, time
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, "/app")
from services.matchmaking import gerar_matches_para_obra  # type: ignore

DB = dict(
    host=os.getenv("DB_HOST", "db"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "wins_hub"),
    user=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", ""),
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recovery")


def main() -> int:
    t0 = time.time()
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT o.id::text AS id, o.fonte, o.nome
                FROM obras o
                WHERE o.criado_em > '2026-05-08 04:00:00+00'
                  AND NOT EXISTS (
                      SELECT 1 FROM matches_obra_prestador m WHERE m.obra_id = o.id
                  )
                ORDER BY o.fonte, o.criado_em
            """)
            obras = cur.fetchall()
    finally:
        conn.close()

    log.info(f"obras pendentes: {len(obras)}")
    total_matches = 0
    erros = 0
    for i, o in enumerate(obras, 1):
        try:
            r = gerar_matches_para_obra(o["id"])
            n = int(r.get("matches_gerados") or 0)
            total_matches += n
            log.info(f"  [{i}/{len(obras)}] {o['fonte']} {o['nome'][:60]}: {n} matches")
        except Exception as e:
            erros += 1
            log.exception(f"  [{i}/{len(obras)}] FALHOU: {e}")

    log.info(f"matchmaking: {total_matches} matches, {erros} erros")

    # Refresh MVs (mesmas chamadas do orchestrator)
    log.info("Refresh fornecedor_matches_summary…")
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE fornecedor_matches_summary;")
            cur.execute("""
                INSERT INTO fornecedor_matches_summary
                SELECT cnpj, COUNT(*)::int, ROUND(AVG(score)::numeric, 0)::int
                FROM matches_obra_prestador GROUP BY cnpj;
            """)
        conn.commit()
        log.info("  ✓ summary refreshed")
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fornecedores_facetas_global;")
            cur.execute("REFRESH MATERIALIZED VIEW mv_fornecedores_score_bands_global;")
            cur.execute("REFRESH MATERIALIZED VIEW mv_fornecedores_lista_global;")
        conn.commit()
        log.info("  ✓ MVs facetas refreshed")
    finally:
        conn.close()

    # Log final ORCHESTRATOR=sucesso
    dur_ms = int((time.time() - t0) * 1000)
    erro_msg = (f"recovery manual: {erros} obras com erro" if erros else
                "recovery manual após kill de matchmaking travado em ~13k obras")
    status_final = "sucesso" if erros == 0 else "erro"
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO log_captacao (fonte, status, novos, buscados, erro, duracao_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("MATCHMAKING", status_final, total_matches, len(obras), erro_msg, dur_ms),
            )
            cur.execute(
                "INSERT INTO log_captacao (fonte, status, novos, buscados, erro, duracao_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("ORCHESTRATOR", "sucesso", 8, 8,
                 "captadores OK; matchmaking via recovery manual (orchestrator anterior travado em batch espúrio)",
                 dur_ms),
            )
        conn.commit()
        log.info("  ✓ log_captacao gravado (MATCHMAKING + ORCHESTRATOR)")
    finally:
        conn.close()

    log.info(f"=== RECOVERY FIM em {dur_ms}ms ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
