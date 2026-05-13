#!/usr/bin/env python3
"""Enriquece ICP Top 500 com decisor via pipeline P3.1.

Le CSV /tmp/outputs/icp_top500_YYYYMMDD.csv → para cada CNPJ chama
descobrir_decisores (camada 3, busca em search engines + filtros Haiku) e
enriquecer_decisores_com_email (camada 4, pattern + Hunter fallback).
Persiste em empresa_decisores_cache.

Cap defensivo: Hunter saldo < 100 → parar.
Idempotente: skip CNPJs que tem decisor recente (<30d).

Saída final: /tmp/outputs/mari_icp_decisores_YYYYMMDD.csv

Uso:
    docker exec wins_hub-api-1 python /app/scripts/enrich_icp_top500.py \\
        --csv /tmp/outputs/icp_top500_YYYYMMDD.csv [--limite 200]
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from typing import List, Tuple

sys.path.insert(0, "/app")

import psycopg2
import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("enrich_icp")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
HUNTER_KEY = os.getenv("HUNTER_API_KEY", "")
HUNTER_FLOOR = 100
SLEEP_ENTRE_CNPJS = 2.0


def hunter_saldo() -> int:
    if not HUNTER_KEY:
        return 0
    try:
        r = requests.get(f"https://api.hunter.io/v2/account?api_key={HUNTER_KEY}", timeout=10)
        d = r.json().get("data", {})
        return int(d.get("calls", {}).get("available", 0)) - int(d.get("calls", {}).get("used", 0))
    except Exception:
        return -1


def decisor_recente_existe(conn, cnpj: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM empresa_decisores_cache
            WHERE cnpj=%s AND atualizado_em > NOW() - INTERVAL '30 days'
            LIMIT 1
        """, (cnpj,))
        return cur.fetchone() is not None


def carregar_cnpjs(csv_path: str, limite: int) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            cnpj = (row.get("cnpj") or "").strip()
            razao = (row.get("razao_social") or "").strip()
            if not cnpj or len(cnpj) != 14:
                continue
            out.append((cnpj, razao))
            if limite and len(out) >= limite:
                break
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--limite", type=int, default=200)
    args = p.parse_args()

    saldo_inicial = hunter_saldo()
    log.info(f"Hunter saldo inicial: {saldo_inicial}")
    if saldo_inicial < HUNTER_FLOOR:
        log.error(f"Hunter saldo {saldo_inicial} abaixo do floor {HUNTER_FLOOR} — abortando")
        return 2

    cnpjs = carregar_cnpjs(args.csv, args.limite)
    log.info(f"CNPJs a processar: {len(cnpjs)}")

    try:
        from sales_intelligence.camada3_decisores.orquestrador import descobrir_decisores
        from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
        from sales_intelligence.db.cache_decisores import gravar_decisor
    except Exception as e:
        log.error(f"import P3.1 falhou: {e}")
        return 3

    conn = psycopg2.connect(**DB_CONFIG)
    stats = {"processados": 0, "sucessos": 0, "ja_cached": 0, "sem_decisor": 0,
             "erros": 0, "hunter_used": 0}
    try:
        for cnpj, razao in cnpjs:
            stats["processados"] += 1
            if decisor_recente_existe(conn, cnpj):
                stats["ja_cached"] += 1
                continue
            try:
                decisores = descobrir_decisores(cnpj, razao, force_refresh=False)
                if not decisores:
                    stats["sem_decisor"] += 1
                    continue
                # Email enrichment (pattern-first, Hunter ultimo recurso)
                try:
                    from sales_intelligence.camada1_identificacao.descobrir_dominio import descobrir_dominio_via_chain
                    dom = descobrir_dominio_via_chain(razao)
                except Exception:
                    dom = None
                if dom:
                    try:
                        decisores = enriquecer_decisores_com_email(
                            cnpj=cnpj, dominio_oficial=dom,
                            decisores=decisores, permitir_hunter=True,
                        )
                    except Exception as e:
                        log.warning(f"  enrichment email {cnpj}: {e}")
                # Gravar
                n = 0
                for d in decisores:
                    try:
                        if gravar_decisor(d):
                            n += 1
                    except Exception as e:
                        log.warning(f"  gravar {cnpj}: {e}")
                if n:
                    stats["sucessos"] += 1
                else:
                    stats["sem_decisor"] += 1
                log.info(f"  [{stats['processados']}/{len(cnpjs)}] {razao[:35]:35} cnpj={cnpj} novos={n}")
            except Exception as e:
                stats["erros"] += 1
                log.warning(f"  erro {cnpj} {razao[:30]}: {e}")
            time.sleep(SLEEP_ENTRE_CNPJS)
            # Hunter floor check a cada 20
            if stats["processados"] % 20 == 0:
                s = hunter_saldo()
                log.info(f"  Hunter saldo: {s}")
                if s < HUNTER_FLOOR:
                    log.warning(f"  Hunter saldo {s} abaixo do floor — parando")
                    break
    finally:
        conn.close()

    saldo_final = hunter_saldo()
    stats["hunter_used"] = (saldo_inicial - saldo_final) if saldo_inicial > 0 else 0
    log.info(f"FIM — {stats} | Hunter saldo final: {saldo_final}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
