#!/usr/bin/env python3
"""Fix CAT3 — descobrir dominios via Camada 1 P3.1 (Serper chain).
Zero Hunter. Popula empresa_dominios pros 18 CNPJs CAT3 sem domínio.
"""
from __future__ import annotations

import logging
import os
import sys
import time
import traceback as _tb
from typing import Optional

sys.path.insert(0, "/app")

import psycopg2


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fix_cat3")

DB = dict(
    host=os.getenv("DB_HOST", "db"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "wins_hub"),
    user=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", ""),
)


def main() -> int:
    from sales_intelligence.camada1_identificacao.descobrir_dominio import descobrir_dominio_via_chain
    conn = psycopg2.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cnpj, empresa, classif, max_valor FROM (
                SELECT o.cnpj,
                       MAX(o.empresa) AS empresa,
                       MIN(CASE o.classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END) AS classif_ord,
                       MAX(CASE o.classificacao_computed WHEN 'OURO' THEN 1 ELSE 2 END)::text AS _x,
                       MAX(o.classificacao_computed) AS classif,
                       MAX(o.valor_estimado) AS max_valor
                FROM obras o
                LEFT JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
                WHERE o.classificacao_computed IN ('OURO','PRATA')
                  AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
                  AND o.nivel1_email IS NULL
                  AND (ed.dominio IS NULL OR ed.dominio = '')
                  AND cnpj_valido(o.cnpj) = TRUE
                GROUP BY o.cnpj
            ) t
            ORDER BY classif_ord, max_valor DESC NULLS LAST
        """)
        rows = cur.fetchall()
    log.info(f"T2: {len(rows)} CNPJs sem dominio")
    sucessos = 0
    falhas = 0
    for i, (cnpj, razao, classif, valor) in enumerate(rows, 1):
        log.info(f"  [{i}/{len(rows)}] {classif} cnpj={cnpj} razao={(razao or '')[:50]}")
        try:
            dom = descobrir_dominio_via_chain(razao or "")
        except Exception as e:
            log.warning(f"    erro chain: {e}")
            falhas += 1
            continue
        if not dom:
            log.info(f"    NAO ENCONTRADO")
            falhas += 1
            continue
        log.info(f"    OK → {dom}")
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO empresa_dominios (
                        cnpj, empresa_nome, dominio, fonte, confianca, dominio_status,
                        validacao_metodo, validacao_data
                    ) VALUES (
                        %s, %s, %s, 'V2_chain', 3, 'ok',
                        'descoberta_automatica_V2', NOW()::date
                    )
                    ON CONFLICT (cnpj) DO UPDATE SET
                        dominio = COALESCE(empresa_dominios.dominio, EXCLUDED.dominio),
                        empresa_nome = COALESCE(empresa_dominios.empresa_nome, EXCLUDED.empresa_nome),
                        validacao_metodo = EXCLUDED.validacao_metodo,
                        validacao_data = EXCLUDED.validacao_data,
                        atualizado_em = NOW()
                """, (cnpj, (razao or "")[:255], dom))
            conn.commit()
            sucessos += 1
        except Exception as e:
            log.warning(f"    persist erro: {e}")
            falhas += 1
            conn.rollback()
        time.sleep(2)
    conn.close()
    log.info(f"FIM: {sucessos} sucessos, {falhas} falhas de {len(rows)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except BaseException as e:
        log.error(f"UNCAUGHT {type(e).__name__}: {e}")
        log.error(_tb.format_exc())
        sys.exit(1)
