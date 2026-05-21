"""
enrich_telefone_brasilapi.py — Popula obras.nivel1_telefone via BrasilAPI Receita.

Fonte: cache_brasilapi.payload->>'ddd_telefone_1' (telefone fixo da Receita Federal).
Defensável 100% — fonte oficial. Telefone GERAL da empresa, não direto do decisor.

Fluxo:
  1. SELECT CNPJs distintos OURO/PRATA visíveis sem telefone
  2. Pra cada: consultar_cnpj (usa cache, faz call BrasilAPI se cache miss)
  3. UPDATE obras.nivel1_telefone em todas obras desse CNPJ
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, "/app")

import psycopg2
from services.brasilapi import consultar_cnpj


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

SLEEP_BETWEEN_CALLS = 22  # BrasilAPI free ~3/min


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT regexp_replace(o.cnpj,'[^0-9]','','g') AS cnpj_full
        FROM obras o
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND COALESCE(o.cnpj,'') <> ''
          AND LENGTH(regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g')) = 14
          AND COALESCE(o.nivel1_telefone,'') = ''
        ORDER BY 1
    """)
    cnpjs = [r[0] for r in cur.fetchall()]
    if args.limit:
        cnpjs = cnpjs[: args.limit]

    print(f"\nTelefone via BrasilAPI: {len(cnpjs)} CNPJs distintos")
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print(f"Tempo estimado: ~{len(cnpjs) * SLEEP_BETWEEN_CALLS / 60:.0f} min\n")

    if not args.commit:
        print("DRY-RUN. Use --commit pra consultar BrasilAPI e atualizar.")
        return

    total_com_tel = 0
    total_sem_tel = 0
    total_erro = 0
    total_cache = 0
    obras_updateadas = 0
    t0 = time.time()

    for i, cnpj in enumerate(cnpjs, 1):
        # consultar_cnpj usa cache local. Se já está no cache, retorna instantâneo.
        try:
            dados = consultar_cnpj(cnpj)
        except Exception as e:
            total_erro += 1
            print(f"[{i:03d}/{len(cnpjs)}] ER {cnpj}: {e!r}")
            time.sleep(SLEEP_BETWEEN_CALLS)
            continue

        if not dados:
            total_sem_tel += 1
            print(f"[{i:03d}/{len(cnpjs)}] -- {cnpj}: vazio/erro BrasilAPI")
            time.sleep(SLEEP_BETWEEN_CALLS)
            continue

        # Cache hit ou miss? consultar_cnpj não nos diz diretamente, mas se foi rápido foi cache
        # Vou só pular sleep se for cache hit (heurística: dt < 1s)
        tel = (dados.get("ddd_telefone_1") or "").strip()
        if not tel:
            tel = (dados.get("ddd_telefone_2") or "").strip()

        if not tel:
            total_sem_tel += 1
            print(f"[{i:03d}/{len(cnpjs)}] -- {cnpj} ({dados.get('razao_social','')[:30]}): sem telefone na Receita")
        else:
            total_com_tel += 1
            cur.execute("""
                UPDATE obras
                   SET nivel1_telefone = %s
                 WHERE regexp_replace(COALESCE(cnpj,''),'[^0-9]','','g') = %s
                   AND classificacao_computed IN ('OURO','PRATA')
                   AND visivel
                   AND COALESCE(nivel1_telefone,'') = ''
            """, (tel, cnpj))
            n = cur.rowcount
            obras_updateadas += n
            print(f"[{i:03d}/{len(cnpjs)}] OK {cnpj} → {tel} ({dados.get('razao_social','')[:25]}) → {n} obras")
        conn.commit()
        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\n{'='*60}")
    print(f"  CNPJs com telefone:   {total_com_tel}")
    print(f"  CNPJs sem telefone:   {total_sem_tel}")
    print(f"  Erros BrasilAPI:      {total_erro}")
    print(f"  Obras UPDATEd:        {obras_updateadas}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
