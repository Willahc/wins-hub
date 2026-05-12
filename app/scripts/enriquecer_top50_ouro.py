#!/usr/bin/env python3
"""P3.1 v2 — Enriquecimento top 50 obras OURO com search C3 + Hunter capeado.

ESTRATÉGIA:
- Dedup por CNPJ (15 únicos em 50 obras: Petrobras=29, Itaipu=9, etc)
- Idempotência: skip CNPJ se já tem ≥3 decisores ativos no cache
- Search C3: descobrir_decisores (LinkedIn/CREA/CVM/DOU + cache 180d)
- Email: enriquecer_decisores_com_email (pattern-first + Hunter fallback)
- Hunter: CAP global HUNTER_CAP_TOTAL (default 10, quota atual 11/50 até 07/06)
- Top 1-10 (maior CAPEX): permite Hunter
- Top 11-50: SOMENTE pattern (sem Hunter)
- Persistência: cache_decisores.gravar_decisor (cnpj + nome_pessoa UNIQUE)
- Marker: registros novos via P3.1 não têm fonte_descoberta=haiku_p3_1
  (gravar_decisor preserva fonte_descoberta original de DecisorBruto).
  Pra distinguir, log inclui hash do script.

Uso:
    docker exec wins_hub-api-1 python /app/scripts/enriquecer_top50_ouro.py            # DRY
    docker exec wins_hub-api-1 python /app/scripts/enriquecer_top50_ouro.py --commit   # REAL
    docker exec wins_hub-api-1 python /app/scripts/enriquecer_top50_ouro.py --hunter-cap 5
"""
import sys
sys.path.insert(0, "/app")

import argparse
import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

LOG_DIR = Path("/app/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"p3_1_enriquecimento_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("p3_1")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

CSV_PATH = "/app/scripts/p3_1_top50_ouro.csv"
MIN_DECISORES_CACHED = 3
HUNTER_RANK_LIMIT = 10  # só rank<=10 (top capex) tenta Hunter


def carregar_top50(path):
    obras = []
    with open(path) as f:
        for r in csv.DictReader(f, delimiter="|"):
            obras.append(r)
    return obras


def cnpjs_unicos_ordenados(obras):
    """Mantém ordem do CSV (já está por CAPEX DESC). rank=1 maior."""
    seen = []
    seenset = set()
    for o in obras:
        cnpj = (o.get("cnpj") or "").strip()
        if not cnpj or cnpj in seenset:
            continue
        seenset.add(cnpj)
        seen.append({"cnpj": cnpj, "empresa": o.get("empresa", ""), "rank": len(seen) + 1})
    return seen


def status_cache(conn, cnpj):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE trabalha_atualmente = true AND excluido_em IS NULL) AS ativos
            FROM empresa_decisores_cache WHERE cnpj = %s
        """, (cnpj,))
        return dict(cur.fetchone() or {"total": 0, "ativos": 0})


def buscar_dominio(conn, cnpj):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT dominio, holding_dominio FROM empresa_dominios WHERE cnpj=%s",
            (cnpj,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row.get("dominio") or row.get("holding_dominio")


def processar_cnpj(conn, cnpj, empresa, rank, hunter_used, hunter_cap, commit):
    """Retorna dict com status + counts.

    Tenta:
    1) skip idempotente
    2) descobrir_decisores (C3 search + cache)
    3) enriquecer com email (Hunter só se rank<=10 e quota disponível)
    4) gravar_decisor (se commit)
    """
    inicio = time.time()
    st = status_cache(conn, cnpj)
    log.info(f"[rank={rank}] [{cnpj}] {empresa[:45]:45s} cache_atual: total={st.get('total',0)} ativos={st.get('ativos',0)}")

    if (st.get("ativos") or 0) >= MIN_DECISORES_CACHED:
        log.info(f"[{cnpj}] SKIP (já tem {st.get('ativos')} ativos)")
        return {"status": "skip_cache", "novos": 0, "hunter_used": 0, "tempo": time.time() - inicio}

    # 1) Descobrir decisores
    try:
        from sales_intelligence.camada3_decisores.orquestrador import descobrir_decisores
    except Exception as e:
        log.error(f"importação descobrir_decisores falhou: {e}")
        return {"status": "import_fail", "novos": 0, "hunter_used": 0, "tempo": time.time() - inicio}

    try:
        decisores = descobrir_decisores(cnpj, empresa, force_refresh=False)
    except Exception as e:
        log.warning(f"[{cnpj}] descobrir_decisores erro: {e}")
        return {"status": "search_error", "novos": 0, "hunter_used": 0, "tempo": time.time() - inicio}

    log.info(f"[{cnpj}] C3 retornou {len(decisores)} decisor(es)")
    if not decisores:
        return {"status": "no_candidates", "novos": 0, "hunter_used": 0, "tempo": time.time() - inicio}

    # 2) Enriquecer emails
    dominio = buscar_dominio(conn, cnpj)
    log.info(f"[{cnpj}] dominio={dominio or '<nenhum>'}")

    permitir_hunter = (rank <= HUNTER_RANK_LIMIT) and (hunter_used < hunter_cap) and bool(dominio)
    hunter_antes = hunter_used

    if dominio:
        try:
            from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
            decisores = enriquecer_decisores_com_email(
                cnpj=cnpj, dominio_oficial=dominio,
                decisores=decisores, permitir_hunter=permitir_hunter,
            )
        except Exception as e:
            log.warning(f"[{cnpj}] enriquecer_email erro: {e}")
    else:
        log.info(f"[{cnpj}] sem dominio — pula enrichment email")

    # 3) Contar Hunter consumido (best-effort: emails com fonte 'hunter')
    hunter_neste = sum(1 for d in decisores if getattr(d, "email_status", None) == "hunter_found")
    if permitir_hunter:
        hunter_used += hunter_neste
    log.info(f"[{cnpj}] permitir_hunter={permitir_hunter} hunter_neste_run={hunter_neste} acumulado={hunter_used}/{hunter_cap}")

    # 4) Persistir
    if not commit:
        log.info(f"[{cnpj}] dry-run — não persistindo ({len(decisores)} candidatos prontos)")
        return {"status": "dry_ok", "novos": len(decisores), "hunter_used": hunter_neste, "tempo": time.time() - inicio}

    try:
        from sales_intelligence.db.cache_decisores import gravar_decisor
        novos = 0
        for d in decisores:
            try:
                if gravar_decisor(d):
                    novos += 1
            except Exception as e:
                log.warning(f"[{cnpj}] gravar {d.nome_pessoa}: {e}")
        log.info(f"[{cnpj}] gravados: {novos}/{len(decisores)}")
        return {"status": "persisted", "novos": novos, "hunter_used": hunter_neste, "tempo": time.time() - inicio}
    except Exception as e:
        log.error(f"[{cnpj}] persist erro: {e}")
        return {"status": "persist_fail", "novos": 0, "hunter_used": hunter_neste, "tempo": time.time() - inicio}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="grava em empresa_decisores_cache")
    parser.add_argument("--hunter-cap", type=int, default=10, help="máx chamadas Hunter (default 10, quota 11/50)")
    parser.add_argument("--limit-cnpjs", type=int, default=0, help="processar só N primeiros CNPJs (0=todos)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info(f"P3.1 ENRIQUECIMENTO TOP 50 OURO v2 — start {datetime.utcnow().isoformat()}")
    log.info(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN'}  Hunter cap: {args.hunter_cap}")
    log.info(f"Log: {LOG_FILE}")
    log.info("=" * 60)

    obras = carregar_top50(CSV_PATH)
    log.info(f"Obras carregadas: {len(obras)}")
    cnpjs = cnpjs_unicos_ordenados(obras)
    log.info(f"CNPJs únicos: {len(cnpjs)}")
    if args.limit_cnpjs > 0:
        cnpjs = cnpjs[: args.limit_cnpjs]
        log.info(f"Limitado a primeiros {len(cnpjs)} CNPJs")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True  # gravar_decisor faz commit interno

    hunter_used = 0
    stats = {"skip_cache": 0, "import_fail": 0, "search_error": 0, "no_candidates": 0,
             "dry_ok": 0, "persisted": 0, "persist_fail": 0}
    novos_total = 0

    try:
        for c in cnpjs:
            try:
                r = processar_cnpj(conn, c["cnpj"], c["empresa"], c["rank"],
                                   hunter_used, args.hunter_cap, args.commit)
                stats[r["status"]] = stats.get(r["status"], 0) + 1
                hunter_used += r.get("hunter_used", 0)
                novos_total += r.get("novos", 0)
            except Exception as e:
                log.exception(f"erro processando {c['cnpj']}: {e}")
            time.sleep(0.5)  # gentil entre CNPJs
    finally:
        conn.close()

    log.info("=" * 60)
    log.info(f"FIM — stats: {stats}")
    log.info(f"Total novos decisores: {novos_total}")
    log.info(f"Hunter consumido nesta run: {hunter_used} (cap era {args.hunter_cap})")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
