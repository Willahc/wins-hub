#!/usr/bin/env python3
"""Captador PNCP Civil 100k.

Objetivo: trazer obras civis pequenas e medias que os captadores PNCP atuais
descartam pelo piso de R$ 10 mi. Mantem idempotencia usando o mesmo
id_externo = "PNCP:<numeroControlePNCP>", portanto nao duplica obras ja
captadas por pncp_obras/pncp_full.

Uso diario no orchestrator:
    python /app/scripts/captar_pncp_civil_100k.py

Primeira carga:
    python /app/scripts/captar_pncp_civil_100k.py --backfill --since 2025-01-01
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator

import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import (  # noqa: E402
    MOD_CONCORRENCIA_ELETRONICA,
    MOD_CONCORRENCIA_PRESENCIAL,
    MOD_MANIFESTACAO_INTERESSE,
    get_conn,
    inserir_obra_pncp,
    record_para_dict_obra,
)


PNCP_PUBLICACAO = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
FONTE = "pncp_civil_100k"
VALOR_MINIMO_CIVIL = 100_000
DIAS_BACK_DAILY = 3
DEFAULT_SINCE = date(2025, 1, 1)
DEFAULT_CHUNK_DAYS = 31
DEFAULT_MAX_PAGES = 80
REQUEST_SLEEP_S = 1.5
RATE_LIMIT_SLEEP_S = 35
MAX_RETRIES = 4

MODALIDADES = (
    MOD_CONCORRENCIA_ELETRONICA,
    MOD_CONCORRENCIA_PRESENCIAL,
    MOD_MANIFESTACAO_INTERESSE,
)

# Termos pensados para obra civil/urbana e etapa antecedente. Inclui projeto
# executivo quando >=100k porque costuma anteceder edital de execucao.
KW_CIVIL = re.compile(
    r"\b("
    r"obra|obras|construc(?:ao|oes)|constru[cç][aã]o|edifica(?:cao|coes)|edifica[cç][aã]o|"
    r"reforma|reformas|amplia(?:cao|coes)|amplia[cç][aã]o|revitaliza(?:cao|coes)|"
    r"pavimenta(?:cao|coes)|pavimenta[cç][aã]o|recapeamento|drenagem|terraplenagem|"
    r"urbaniza(?:cao|coes)|urbaniza[cç][aã]o|infraestrutura|engenharia|empreitada|"
    r"projeto\s+(?:basico|b[aá]sico|executivo)|servicos?\s+de\s+engenharia|"
    r"escola|creche|hospital|ubs|upa|unidade\s+basica|unidade\s+b[aá]sica|"
    r"quadra|ginasio|gin[aá]sio|praca|pra[cç]a|ponte|viaduto|passarela|"
    r"habitacional|moradia|loteamento|saneamento|esgoto|adutora|ete|eta"
    r")\b",
    re.IGNORECASE,
)

KW_NEGATIVA = re.compile(
    r"\b("
    r"aquisicao|aquisi[cç][aã]o|compra|fornecimento|loca[cç][aã]o|aluguel|"
    r"material\s+de\s+construc|cimento|areia|brita|tinta|ferramentas?|"
    r"merenda|medicamento|veiculo|ve[ií]culo|combustivel|combust[ií]vel|"
    r"limpeza|vigilancia|vigil[aâ]ncia|software|licen[cç]a\s+de\s+software"
    r")\b",
    re.IGNORECASE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_pncp_civil_100k")

_STATS = {
    "buscados": 0,
    "novos": 0,
    "erros": 0,
    "filtrados_capex": 0,
    "filtrados_keyword": 0,
    "filtrados_negativa": 0,
    "duplicados": 0,
}


@atexit.register
def _emit_stats() -> None:
    print(f"STATS_JSON: {json.dumps(_STATS)}", flush=True)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def dateranges(start: date, end: date, chunk_days: int) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=chunk_days - 1))
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def fetch_pncp_publicacao(
    *,
    modalidade: int,
    data_inicial: date,
    data_final: date,
    max_pages: int,
) -> Iterator[Dict[str, Any]]:
    sess = requests.Session()
    pagina = 1
    while pagina <= max_pages:
        url = (
            f"{PNCP_PUBLICACAO}"
            f"?dataInicial={data_inicial:%Y%m%d}"
            f"&dataFinal={data_final:%Y%m%d}"
            f"&codigoModalidadeContratacao={modalidade}"
            f"&pagina={pagina}"
        )
        payload = None
        for tentativa in range(1, MAX_RETRIES + 1):
            try:
                r = sess.get(url, timeout=60)
                if r.status_code == 204 or not r.content:
                    return
                if r.status_code == 429:
                    espera = RATE_LIMIT_SLEEP_S * tentativa
                    log.warning(
                        "PNCP 429 mod=%s %s..%s pag=%s tentativa=%s/%s; aguardando %ss",
                        modalidade, data_inicial, data_final, pagina,
                        tentativa, MAX_RETRIES, espera,
                    )
                    time.sleep(espera)
                    continue
                r.raise_for_status()
                payload = r.json()
                break
            except Exception as exc:
                if tentativa < MAX_RETRIES:
                    espera = 5 * tentativa
                    log.warning(
                        "PNCP falhou mod=%s %s..%s pag=%s tentativa=%s/%s: %s; retry em %ss",
                        modalidade, data_inicial, data_final, pagina,
                        tentativa, MAX_RETRIES, exc, espera,
                    )
                    time.sleep(espera)
                    continue
                log.warning("PNCP falhou mod=%s %s..%s pag=%s: %s",
                            modalidade, data_inicial, data_final, pagina, exc)
                _STATS["erros"] += 1
                return

        if payload is None:
            _STATS["erros"] += 1
            return

        if not isinstance(payload, dict):
            return
        for rec in payload.get("data") or []:
            yield rec

        total_paginas = int(payload.get("totalPaginas") or 0)
        if pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(REQUEST_SLEEP_S)


def _valor(rec: dict) -> float:
    try:
        return float(rec.get("valorTotalEstimado") or 0)
    except (TypeError, ValueError):
        return 0.0


def _objeto(rec: dict) -> str:
    return (rec.get("objetoCompra") or "").strip()


def passa_filtro_civil(rec: dict, modalidade: int, valor_minimo: float) -> bool:
    valor = _valor(rec)
    if valor < valor_minimo:
        _STATS["filtrados_capex"] += 1
        return False

    objeto = _objeto(rec)
    if not objeto:
        _STATS["filtrados_keyword"] += 1
        return False

    # MIP pode ser cedo, mas ainda precisa sinalizar obra civil para nao virar
    # deposito generico de credenciamento/estudo sem obra.
    if not KW_CIVIL.search(objeto):
        _STATS["filtrados_keyword"] += 1
        return False
    if KW_NEGATIVA.search(objeto) and not re.search(
        r"obra|construc|reforma|amplia|pavimenta|engenharia|projeto",
        objeto,
        re.IGNORECASE,
    ):
        _STATS["filtrados_negativa"] += 1
        return False
    return True


def ajustar_dados_civil(dados: dict, rec: dict, modalidade: int) -> dict:
    objeto = _objeto(rec)
    low = objeto.lower()
    dados["fonte"] = FONTE
    if "projeto executivo" in low or "projeto básico" in low or "projeto basico" in low:
        dados["_fase_override"] = "PROJETO"
    elif modalidade == MOD_MANIFESTACAO_INTERESSE:
        dados["_fase_override"] = "PLANEJAMENTO"
    else:
        dados["_fase_override"] = "LICITACAO_ABERTA"

    if any(k in low for k in ("esgoto", "saneamento", "adutora", " ete", " eta", "drenagem")):
        dados["setor"] = "SANEAMENTO"
    else:
        dados["setor"] = "INFRAESTRUTURA"

    dados["descricao"] = (objeto[:900] + "\n\nFonte: PNCP Civil 100k").strip()
    return dados


def inserir_obra_civil(conn, dados: Dict[str, Any], dry: bool = False) -> str | None:
    if dry:
        return "dry-run"
    fase = dados.pop("_fase_override", None) or "LICITACAO_ABERTA"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica, id_externo
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, 'OFICIAL',
                %s, 'anunciado', %s, 0.88,
                %s, false, %s
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
            """,
            (
                dados["nome"], dados["empresa"], dados["cnpj"],
                dados["uf"], dados["municipio"], dados["setor"],
                dados["valor_estimado"], fase, dados["fonte"],
                dados["url_fonte"], dados["data_anuncio"],
                dados["descricao"], dados["id_externo"],
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--since", type=parse_date, default=DEFAULT_SINCE)
    parser.add_argument("--until", type=parse_date, default=date.today())
    parser.add_argument("--days", type=int, default=DIAS_BACK_DAILY)
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--valor-min", type=float, default=VALOR_MINIMO_CIVIL)
    args = parser.parse_args()

    if args.backfill:
        inicio = args.since
        fim = args.until
    else:
        fim = date.today()
        inicio = fim - timedelta(days=max(1, args.days))

    log.info(
        "PNCP civil 100k — janela %s -> %s | valor >= R$ %.0f | max_pages/chunk=%s%s",
        inicio, fim, args.valor_min, args.max_pages, " [DRY]" if args.dry else "",
    )

    conn = None if args.dry else get_conn()
    amostra = []
    try:
        for data_ini, data_fim in dateranges(inicio, fim, max(1, args.chunk_days)):
            for modalidade in MODALIDADES:
                for rec in fetch_pncp_publicacao(
                    modalidade=modalidade,
                    data_inicial=data_ini,
                    data_final=data_fim,
                    max_pages=args.max_pages,
                ):
                    _STATS["buscados"] += 1
                    if not passa_filtro_civil(rec, modalidade, args.valor_min):
                        continue
                    dados = record_para_dict_obra(rec, fonte=FONTE)
                    if not dados:
                        continue
                    dados = ajustar_dados_civil(dados, rec, modalidade)
                    if args.dry and len(amostra) < 12:
                        amostra.append({
                            "id_externo": dados["id_externo"],
                            "valor": float(dados["valor_estimado"] or 0),
                            "uf": dados["uf"],
                            "nome": dados["nome"][:90],
                        })
                    try:
                        oid = inserir_obra_civil(conn, dados, dry=args.dry)
                        if oid and oid != "dry-run":
                            _STATS["novos"] += 1
                        elif oid == "dry-run":
                            _STATS["novos"] += 1
                        else:
                            _STATS["duplicados"] += 1
                    except Exception as exc:
                        log.warning("insert erro %s: %s", dados.get("id_externo"), exc)
                        _STATS["erros"] += 1
                        if conn:
                            conn.rollback()
    finally:
        if conn:
            conn.close()

    for item in amostra:
        log.info("AMOSTRA %s | R$ %.0f | %s | %s",
                 item["id_externo"], item["valor"], item["uf"], item["nome"])
    log.info("PNCP civil 100k done — %s", _STATS)


if __name__ == "__main__":
    main()
