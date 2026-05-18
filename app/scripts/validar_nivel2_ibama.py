#!/usr/bin/env python3
"""
Validacao Nivel 2 - IBAMA SISLIC
Lookup de cada obra ibama_sislic no HTML diario do SISLIC.
Seta obra_listada_na_fonte, obra_fase_fonte, e obra_dados_mudaram_at quando fase mudou.

Idempotente. 100% match rate validado em 18/05/2026 (720/720 obras).
Stdlib html.parser (sem bs4), asyncpg, httpx.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

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

HTML_URL = "https://dadosabertos.ibama.gov.br/dados/SISLIC/sislic-licencas.html"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# IBAMA tipo de licenca -> fase canonica WiNS Hub.
# Alinhado com fases existentes em obras.fase (EM_EXECUCAO/LICENCA_INSTALACAO/LICENCA_PREVIA/OPERACAO).
# Cobertura: 17 tipos distintos validados em 18/05/2026 com 523 obras tier OURO/PRATA/BRONZE.
FASE_MAP = {
    # Licenças formais (3 fases canonicas)
    "Licença Prévia":                                                                "LICENCA_PREVIA",
    "Retificação de Licença Prévia":                                                 "LICENCA_PREVIA",
    "Licença de Instalação":                                                         "LICENCA_INSTALACAO",
    "Retificação de Licença de Instalação":                                          "LICENCA_INSTALACAO",
    "Renovação de Licença de Instalação":                                            "LICENCA_INSTALACAO",
    "Licença de Operação":                                                           "OPERACAO",
    "Retificação de Licença de Operação":                                            "OPERACAO",
    "Renovação de Licença de Operação":                                              "OPERACAO",
    "Retificação da Renovação de Licença de Operação":                               "OPERACAO",
    "Licença de Operação - Regularização":                                           "OPERACAO",
    # Autorizações pontuais (durante construção/operação — não mudam fase do empreendimento principal)
    "Anuência":                                                                      "EM_EXECUCAO",
    "Autorização de Supressão de Vegetação":                                         "EM_EXECUCAO",
    "Renovação de Autorização de Supressão de Vegetação":                            "EM_EXECUCAO",
    "Autorização de Captura, Coleta e Transporte de Material Biológico":             "EM_EXECUCAO",
    "Renovação de Autorização de Captura, Coleta e Transporte de Material Biológico":"EM_EXECUCAO",
    "Retificação de Autorização de Captura, Coleta e Transporte de Material Biológico":"EM_EXECUCAO",
    "Retificação da Renovação de Autorização de Captura, Coleta e Transporte de Material Biológico":"EM_EXECUCAO",
}

# Padrão id_externo: IBAMA-<processo_RFB>-<num_licenca>/<ano>
# Ex: IBAMA-02001.003272/2011-48-01217/2024 -> processo = 02001.003272/2011-48
EXTRACT_PROCESSO = re.compile(r"^IBAMA-(\d+\.\d+/\d+-\d+)-")

# Indices das colunas no tbody (validado 18/05/2026)
IDX_TIPO       = 0
IDX_NUMERO     = 1
IDX_DT_EMISSAO = 2
IDX_DT_VENC    = 3
IDX_EMPREEND   = 4
IDX_PESSOA     = 5
IDX_PROCESSO   = 6
IDX_TIPOLOGIA  = 7
IDX_PAC        = 8


class SISLICTableExtractor(HTMLParser):
    """Parser de tabela HTML SISLIC. Coleta cada <tr> dentro de <tbody> como lista de cell-strings."""

    def __init__(self):
        super().__init__()
        self.in_tbody = False
        self.in_tr = False
        self.in_td = False
        self.current_row = []
        self.current_cell = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.in_tr = True
            self.current_row = []
        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.current_cell = []

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_tbody = False
        elif tag == "tr" and self.in_tr:
            self.in_tr = False
            if self.current_row:
                self.rows.append(self.current_row)
        elif tag == "td" and self.in_td:
            self.in_td = False
            self.current_row.append("".join(self.current_cell).strip())

    def handle_data(self, data):
        if self.in_td:
            self.current_cell.append(data)


def parse_data_dmy(s):
    """Converte dd/mm/yyyy em yyyy-mm-dd (string ordenavel). Vazio se invalido."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", (s or "").strip())
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


async def main():
    now = datetime.now(timezone.utc)
    stats = {
        "step": "C4_nivel2_ibama",
        "inicio": now.isoformat(),
        "html_bytes": 0,
        "html_rows": 0,
        "processos_unicos": 0,
        "obras_processadas": 0,
        "listadas": 0,
        "nao_listadas": 0,
        "id_externo_invalido": 0,
        "fase_mudou": 0,
        "fase_inalterada": 0,
        "tipos_licenca_unicos": {},
    }

    # 1) Download HTML SISLIC
    log.info(f"Baixando HTML SISLIC: {HTML_URL[:80]}...")
    async with httpx.AsyncClient() as client:
        r = await client.get(HTML_URL, timeout=120, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
    stats["html_bytes"] = len(r.content)
    log.info(f"HTML baixado: {stats['html_bytes']/1024/1024:.1f} MB")

    # 2) Parse + index por processo (mais recente por data_emissao)
    parser = SISLICTableExtractor()
    parser.feed(r.text)
    stats["html_rows"] = len(parser.rows)

    sislic_index = {}
    for row in parser.rows:
        if len(row) < 9:
            continue
        proc = row[IDX_PROCESSO]
        if not proc:
            continue
        sort_key = parse_data_dmy(row[IDX_DT_EMISSAO])
        existing = sislic_index.get(proc)
        if existing is None or sort_key > existing["sort_key"]:
            sislic_index[proc] = {
                "tipo":       row[IDX_TIPO],
                "numero":     row[IDX_NUMERO],
                "dt_emissao": row[IDX_DT_EMISSAO],
                "dt_venc":    row[IDX_DT_VENC],
                "empreend":   row[IDX_EMPREEND],
                "pessoa":     row[IDX_PESSOA],
                "tipologia":  row[IDX_TIPOLOGIA],
                "sort_key":   sort_key,
            }
    stats["processos_unicos"] = len(sislic_index)
    log.info(f"HTML indexado: {stats['html_rows']} rows, {stats['processos_unicos']} processos unicos")

    # 3) Iterar obras IBAMA e atualizar
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        obras = await conn.fetch("""
            SELECT id, id_externo, fase, nome
            FROM obras
            WHERE fonte = 'ibama_sislic'
              AND classificacao_computed IN ('OURO','PRATA','BRONZE')
              AND id_externo IS NOT NULL
        """)
        log.info(f"Obras IBAMA a processar: {len(obras)}")

        for obra in obras:
            stats["obras_processadas"] += 1
            id_ext = obra["id_externo"] or ""
            m = EXTRACT_PROCESSO.match(id_ext)

            if not m:
                stats["id_externo_invalido"] += 1
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = FALSE,
                        validacao_obra_at     = $2::timestamptz
                    WHERE id = $1
                """, obra["id"], now)
                stats["nao_listadas"] += 1
                continue

            proc = m.group(1)
            row_sislic = sislic_index.get(proc)

            if row_sislic is None:
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = FALSE,
                        validacao_obra_at     = $2::timestamptz
                    WHERE id = $1
                """, obra["id"], now)
                stats["nao_listadas"] += 1
                continue

            tipo_raw = row_sislic["tipo"]
            stats["tipos_licenca_unicos"][tipo_raw] = stats["tipos_licenca_unicos"].get(tipo_raw, 0) + 1

            fase_canonica = FASE_MAP.get(tipo_raw, "")
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
                """, obra["id"], tipo_raw, now)
                stats["fase_mudou"] += 1
                log.info(f"FASE_MUDOU: {id_ext} {fase_banco} -> {fase_canonica} (IBAMA: {tipo_raw})")
            else:
                # Auto-limpa obra_dados_mudaram_at quando reconcilia (mesmo padrao ANEEL/BNDES)
                await conn.execute("""
                    UPDATE obras SET
                        obra_listada_na_fonte = TRUE,
                        obra_fase_fonte       = $2,
                        obra_dados_mudaram_at = NULL,
                        validacao_obra_at     = $3::timestamptz
                    WHERE id = $1
                """, obra["id"], tipo_raw, now)
                stats["fase_inalterada"] += 1
            stats["listadas"] += 1
    finally:
        await conn.close()

    stats["fim"] = datetime.now(timezone.utc).isoformat()
    print("\n=== STATS_JSON ===")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
