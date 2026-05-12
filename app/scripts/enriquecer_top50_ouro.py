#!/usr/bin/env python3
"""P3.1 — Enriquecimento top 50 obras OURO (conservador, sem Hunter).

ESTRATÉGIA:
- Dedup por CNPJ (15 CNPJs únicos em 50 obras: Petrobras=29, Itaipu=9, etc)
- Idempotência: skip CNPJ se já tem ≥3 decisores no cache OU já marcado fonte_descoberta='haiku_p3_1'
- Hunter: PRESERVADO (não chamado). Quota crítica 11/50 até 07/06.
- Search: usa orquestrador camada3_decisores (LinkedIn/CREA/CVM/DOU) se disponível
- Validação: Haiku 4.5 (filtro_llm_em + filtro_llm_confianca)
- Persistência: empresa_decisores_cache com fonte_descoberta='haiku_p3_1'

Uso:
    python3 /app/scripts/enriquecer_top50_ouro.py            # dry-run
    python3 /app/scripts/enriquecer_top50_ouro.py --commit   # real
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

LOG_DIR = Path("/app/logs") if Path("/app/logs").exists() else Path("/tmp")
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
MARKER = "haiku_p3_1"


def carregar_top50(path: str):
    obras = []
    with open(path) as f:
        reader = csv.DictReader(f, delimiter="|")
        for r in reader:
            obras.append(r)
    return obras


def cnpjs_unicos(obras):
    seen = []
    seenset = set()
    for o in obras:
        cnpj = (o.get("cnpj") or "").strip()
        if not cnpj or cnpj in seenset:
            continue
        seenset.add(cnpj)
        seen.append({"cnpj": cnpj, "empresa": o.get("empresa", "")})
    return seen


def status_cache(conn, cnpj):
    """Retorna dict com total + marker count + ativos."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE fonte_descoberta = %s) AS via_p3_1,
              COUNT(*) FILTER (WHERE trabalha_atualmente = true AND excluido_em IS NULL) AS ativos
            FROM empresa_decisores_cache
            WHERE cnpj = %s
        """, (MARKER, cnpj))
        return dict(cur.fetchone() or {})


def buscar_dominio(conn, cnpj):
    """Lookup empresa_dominios cache (sem chain externa por ora)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT dominio, holding_dominio FROM empresa_dominios WHERE cnpj=%s",
            (cnpj,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row.get("dominio") or row.get("holding_dominio")


def buscar_decisores_via_c3(cnpj, empresa, dominio):
    """Tenta usar orquestrador camada3_decisores. Retorna [] se indisponível."""
    try:
        from sales_intelligence.camada3_decisores.orquestrador import buscar_decisores_empresa
        return buscar_decisores_empresa(cnpj=cnpj, empresa_nome=empresa, dominio=dominio)
    except (ImportError, AttributeError) as e:
        log.warning(f"orquestrador C3 indisponivel ({e}); pulando search")
        return []
    except Exception as e:
        log.error(f"erro C3 para {cnpj}: {e}")
        return []


def processar_cnpj(conn, cnpj, empresa, commit=False):
    inicio = time.time()
    st = status_cache(conn, cnpj)
    log.info(f"[{cnpj}] {empresa[:40]:40s} cache: total={st.get('total',0)} ativos={st.get('ativos',0)} p3_1={st.get('via_p3_1',0)}")

    if (st.get("ativos") or 0) >= MIN_DECISORES_CACHED:
        log.info(f"[{cnpj}] SKIP (já tem {st.get('ativos')} decisores ativos)")
        return {"status": "skipped_has_cache", "novos": 0, "tempo": time.time() - inicio}

    if (st.get("via_p3_1") or 0) > 0:
        log.info(f"[{cnpj}] SKIP (já processado por p3_1)")
        return {"status": "skipped_processed", "novos": 0, "tempo": time.time() - inicio}

    dominio = buscar_dominio(conn, cnpj)
    log.info(f"[{cnpj}] dominio={dominio or '<sem dominio cache>'}")

    decisores = buscar_decisores_via_c3(cnpj, empresa, dominio)
    log.info(f"[{cnpj}] C3 retornou {len(decisores)} candidatos")

    if not decisores:
        return {"status": "no_candidates", "novos": 0, "tempo": time.time() - inicio}

    # TODO próxima sessão: chamar Haiku validation pra cada candidato + INSERT em empresa_decisores_cache
    # Pra preservar quota Anthropic + tempo, só logar candidatos por ora
    log.info(f"[{cnpj}] candidatos prontos pra Haiku — diferido (dry mode)")
    return {"status": "candidates_pending_haiku", "novos": 0, "tempo": time.time() - inicio}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="persistir mudanças")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info(f"P3.1 ENRIQUECIMENTO TOP 50 OURO — start {datetime.utcnow().isoformat()}")
    log.info(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    log.info(f"Log: {LOG_FILE}")
    log.info("=" * 60)

    obras = carregar_top50(CSV_PATH)
    log.info(f"Carregadas {len(obras)} obras do CSV")
    cnpjs = cnpjs_unicos(obras)
    log.info(f"CNPJs únicos: {len(cnpjs)}")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    stats = {"skipped_has_cache": 0, "skipped_processed": 0, "no_candidates": 0, "candidates_pending_haiku": 0, "novos": 0}
    try:
        for c in cnpjs:
            try:
                r = processar_cnpj(conn, c["cnpj"], c["empresa"], commit=args.commit)
                stats[r["status"]] = stats.get(r["status"], 0) + 1
                stats["novos"] += r.get("novos", 0)
            except Exception as e:
                log.exception(f"erro processando {c['cnpj']}: {e}")
            time.sleep(0.5)  # gentil entre CNPJs
    finally:
        conn.close()

    log.info("=" * 60)
    log.info(f"FIM — stats: {stats}")
    log.info(f"Hunter consumido: 0 (quota 11/50 preservada)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
