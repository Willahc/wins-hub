#!/usr/bin/env python3
"""
Validacao Nivel 2 — ANEEL SIGA
Lookup de cada obra aneel_siga no CSV diario do SIGA.
Seta obra_listada_na_fonte=True/False, obra_fase_fonte, e obra_dados_mudaram_at quando fase mudou.

Idempotente. URL CSV real validada via CKAN package_show 17/05/2026.
"""
import asyncio
import csv
import io
import json
import logging
import os
from datetime import datetime, timezone

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST", "db"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "database": os.getenv("DB_NAME", "wins_hub"),
}

# URL real validada em 17/05/2026 via package_show
CSV_URL = "https://dadosabertos.aneel.gov.br/dataset/6d90b77c-c5f5-4d81-bdec-7bc619494bb9/resource/2f65a1b0-19b8-4360-8238-b34ab4693d55/download/siga-empreendimentos-geracao-diario.csv"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# Mapeamento fase SIGA -> nossa fase canonica
FASE_MAP = {
    "Operação":                "OPERACAO",
    "Construção":              "EM_EXECUCAO",
    "Construção não iniciada": "PLANEJAMENTO",
    "Outorgada":               "LICENCA_INSTALACAO",
    "Cancelada":               "CANCELADA",
    "Nao Solicitada":          "PIPELINE",
    "Não Solicitada":          "PIPELINE",
}


async def main():
    now = datetime.now(timezone.utc)
    stats = {
        "step": "B4_nivel2_aneel",
        "inicio": now.isoformat(),
        "csv_rows": 0,
        "obras_processadas": 0,
        "listadas": 0,
        "nao_listadas": 0,
        "fase_mudou": 0,
        "fase_inalterada": 0,
    }

    # 1) Download CSV SIGA diario
    log.info(f"Baixando CSV SIGA: {CSV_URL[:80]}...")
    async with httpx.AsyncClient() as client:
        r = await client.get(CSV_URL, timeout=120, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
    log.info(f"CSV baixado: {len(r.content)/1024/1024:.1f} MB")

    # 2) Parse (ISO-8859-1, delim ;)
    text = r.content.decode("iso-8859-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    siga_index = {}
    for row in reader:
        cod = (row.get("CodCEG") or "").strip()
        if cod:
            siga_index[cod] = row
            stats["csv_rows"] += 1
    log.info(f"CSV indexado: {stats['csv_rows']} registros")

    # 3) Iterar obras ANEEL e fazer lookup
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        obras = await conn.fetch("""
            SELECT id, id_externo, fase, nome
            FROM obras
            WHERE fonte = 'aneel_siga'
              AND classificacao_computed IN ('OURO','PRATA','BRONZE')
              AND id_externo IS NOT NULL
        """)
        log.info(f"Obras ANEEL a processar: {len(obras)}")

        for obra in obras:
            stats["obras_processadas"] += 1
            id_ext = obra["id_externo"] or ""
            cod_ceg = id_ext.removeprefix("ANEEL-").strip()
            row_siga = siga_index.get(cod_ceg)

            if row_siga is None:
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = FALSE,
                        validacao_obra_at = $2::timestamptz
                    WHERE id = $1
                """, obra["id"], now)
                stats["nao_listadas"] += 1
                continue

            fase_siga_raw = (row_siga.get("DscFaseUsina") or "").strip()
            fase_canonica = FASE_MAP.get(fase_siga_raw, fase_siga_raw)
            fase_banco = obra["fase"] or ""
            fase_mudou = bool(fase_canonica and fase_canonica != fase_banco)

            if fase_mudou:
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = TRUE,
                        obra_fase_fonte       = $2,
                        obra_dados_mudaram_at = $3::timestamptz,
                        validacao_obra_at     = $3::timestamptz
                    WHERE id = $1
                """, obra["id"], fase_siga_raw, now)
                stats["fase_mudou"] += 1
                log.info(f"FASE_MUDOU: {cod_ceg} {fase_banco} -> {fase_canonica} (siga: {fase_siga_raw})")
            else:
                # Quando fase reconcilia: limpa obra_dados_mudaram_at (badge UX
                # "Status pode ter mudado" some quando user/auto reconcilia a fase).
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = TRUE,
                        obra_fase_fonte       = $2,
                        obra_dados_mudaram_at = NULL,
                        validacao_obra_at     = $3::timestamptz
                    WHERE id = $1
                """, obra["id"], fase_siga_raw, now)
                stats["fase_inalterada"] += 1

            stats["listadas"] += 1
    finally:
        await conn.close()

    stats["fim"] = datetime.now(timezone.utc).isoformat()
    print("\n=== STATS_JSON ===")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
