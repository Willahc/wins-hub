#!/usr/bin/env python3
"""
Validacao Nivel 2 — BNDES Operacoes de Financiamento (nao automaticas).
Lookup de cada obra bndes_financiamento/bndes_saneamento no CSV (19MB) por (CNPJ, numero_contrato).
Seta obra_listada_na_fonte=True/False e obra_fase_fonte=situacao_do_contrato.

URL CSV validada via CKAN package_show 17/05/2026 (operacoes-financiamento dataset).
NOTA: nao usamos o CSV "indiretas automaticas" (1.18GB) porque nossas obras tier-tier
sao contratos negociados caso a caso (cobertura esperada >90% no CSV nao_auto).
"""
import asyncio
import csv
import io
import json
import logging
import os
import re
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

CSV_URL = "https://dadosabertos.bndes.gov.br/dataset/10e21ad1-568e-45e5-a8af-43f2c05ef1a2/resource/6f56b78c-510f-44b6-8274-78a5b7e931f4/download/operacoes-financiamento-operacoes-nao-automaticas.csv"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# Mapeamento situacao_do_contrato BNDES -> fase canonica WiNS Hub.
# ATIVO  = contrato em andamento (financiamento ainda nao pago) -> obra geralmente em execucao
# LIQUIDADO = contrato totalmente quitado -> projeto entregue, obra em operacao
# Sem entrada = ignora (nao mapeia, nao detecta mudanca)
SITUACAO_MAP = {
    "ATIVO":     "EM_EXECUCAO",
    "LIQUIDADO": "OPERACAO",
}


def clean_cnpj(s):
    return re.sub(r"\D", "", s or "")


def parse_id_externo(id_ext):
    """
    Parse formatos:
      BNDES-<CNPJ>-<numero>           -> ('xxx', 'yyy')
      BNDES-SAN-<CNPJ>-<numero>       -> ('xxx', 'yyy')
    """
    if not id_ext:
        return None, None
    parts = id_ext.split("-")
    if len(parts) < 3:
        return None, None
    if parts[1] == "SAN":
        if len(parts) < 4:
            return None, None
        cnpj_raw = parts[2]
        numero = "-".join(parts[3:])
    else:
        cnpj_raw = parts[1]
        numero = "-".join(parts[2:])
    return clean_cnpj(cnpj_raw), numero.strip()


async def main():
    now = datetime.now(timezone.utc)
    stats = {
        "step": "C3_nivel2_bndes",
        "inicio": now.isoformat(),
        "csv_rows": 0,
        "csv_unique_cnpj_num": 0,
        "obras_processadas": 0,
        "listadas": 0,
        "nao_listadas": 0,
        "id_externo_invalido": 0,
        "situacoes_unicas": {},
    }

    # 1) Download
    log.info(f"Baixando CSV BNDES nao_auto: {CSV_URL[:80]}...")
    async with httpx.AsyncClient() as client:
        r = await client.get(CSV_URL, timeout=120, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
    log.info(f"CSV baixado: {len(r.content) / 1024 / 1024:.1f} MB")

    # 2) Parse + index
    text = r.content.decode("iso-8859-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    index = {}
    for row in reader:
        cnpj_c = clean_cnpj(row.get("cnpj"))
        num = (row.get("numero_do_contrato") or "").strip()
        if cnpj_c and num:
            index[(cnpj_c, num)] = row
            stats["csv_rows"] += 1
    stats["csv_unique_cnpj_num"] = len(index)
    log.info(f"CSV indexado: {stats['csv_rows']} contratos, {stats['csv_unique_cnpj_num']} (cnpj,num) chaves")

    # 3) Iterar obras BNDES
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        obras = await conn.fetch("""
            SELECT id, id_externo, fase, nome
            FROM obras
            WHERE fonte IN ('bndes_financiamento', 'bndes_saneamento')
              AND classificacao_computed IN ('OURO','PRATA','BRONZE')
              AND id_externo IS NOT NULL
        """)
        log.info(f"Obras BNDES a processar: {len(obras)}")

        for obra in obras:
            stats["obras_processadas"] += 1
            cnpj_clean, num = parse_id_externo(obra["id_externo"])

            if not cnpj_clean or not num:
                stats["id_externo_invalido"] += 1
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = FALSE,
                        validacao_obra_at     = $2::timestamptz
                    WHERE id = $1
                """, obra["id"], now)
                stats["nao_listadas"] += 1
                continue

            row_bndes = index.get((cnpj_clean, num))
            if row_bndes is None:
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = FALSE,
                        validacao_obra_at     = $2::timestamptz
                    WHERE id = $1
                """, obra["id"], now)
                stats["nao_listadas"] += 1
                continue

            situacao = (row_bndes.get("situacao_do_contrato") or "").strip()
            stats["situacoes_unicas"][situacao] = stats["situacoes_unicas"].get(situacao, 0) + 1

            # Detectar mudanca de fase: situacao_BNDES mapeada vs fase atual no banco
            fase_canonica = SITUACAO_MAP.get(situacao)
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
                """, obra["id"], situacao, now)
                stats.setdefault("fase_mudou", 0)
                stats["fase_mudou"] += 1
                log.info(f"FASE_MUDOU: {obra['id_externo']} {fase_banco} -> {fase_canonica} (BNDES: {situacao})")
            else:
                # Auto-limpa obra_dados_mudaram_at quando reconcilia (similar ao ANEEL)
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = TRUE,
                        obra_fase_fonte       = $2,
                        obra_dados_mudaram_at = NULL,
                        validacao_obra_at     = $3::timestamptz
                    WHERE id = $1
                """, obra["id"], situacao, now)
                stats.setdefault("fase_inalterada", 0)
                stats["fase_inalterada"] += 1
            stats["listadas"] += 1
    finally:
        await conn.close()

    stats["fim"] = datetime.now(timezone.utc).isoformat()
    print("\n=== STATS_JSON ===")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
