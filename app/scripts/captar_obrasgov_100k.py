#!/usr/bin/env python3
"""Captador ObrasGov 100k.

Fonte oficial nacional para projetos de investimento em infraestrutura.
API documentada em:
https://api.obrasgov.gestao.gov.br/obrasgov/api/swagger-ui/index.html

Filtra natureza=Obra e soma de fontesDeRecurso >= R$ 100 mil.
Idempotencia: id_externo = "OBRASGOV:<idUnico>".
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
from typing import Any, Dict, Iterator, Optional

import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import get_conn, setor_de_objeto  # noqa: E402


BASE_URL = "https://api.obrasgov.gestao.gov.br/obrasgov/api/projeto-investimento"
FONTE = "obrasgov_100k"
VALOR_MINIMO = 100_000
PAGE_SIZE = 100
REQUEST_SLEEP_S = 0.4
RATE_LIMIT_SLEEP_S = 30
MAX_RETRIES = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_obrasgov_100k")

_STATS = {
    "buscados": 0,
    "novos": 0,
    "erros": 0,
    "filtrados_capex": 0,
    "filtrados_natureza": 0,
    "duplicados": 0,
}


@atexit.register
def _emit_stats() -> None:
    print(f"STATS_JSON: {json.dumps(_STATS)}", flush=True)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_dates(start: date, end: date) -> Iterator[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def valor_previsto(rec: dict) -> float:
    total = 0.0
    for fonte in rec.get("fontesDeRecurso") or []:
        try:
            total += float(fonte.get("valorInvestimentoPrevisto") or 0)
        except (TypeError, ValueError):
            pass
    return total


def primeiro_nome_codigo(items: list[dict]) -> tuple[Optional[str], Optional[str]]:
    if not items:
        return None, None
    item = items[0] or {}
    codigo = item.get("codigo")
    return item.get("nome"), str(codigo) if codigo else None


def fase_de_situacao(situacao: str | None, data_ini_efetiva: str | None) -> str:
    s = (situacao or "").lower()
    if data_ini_efetiva or "execu" in s or "iniciad" in s:
        return "EM_EXECUCAO"
    if "conclu" in s or "finaliz" in s:
        return "OPERACIONAL"
    return "PLANEJAMENTO"


def setor_obrasgov(rec: dict) -> str:
    texto = " ".join(
        str(x or "")
        for x in (
            rec.get("nome"),
            rec.get("descricao"),
            rec.get("metaGlobal"),
            " ".join(t.get("descricao", "") for t in rec.get("tipos") or []),
            " ".join(t.get("descricao", "") for t in rec.get("subTipos") or []),
        )
    )
    setor = setor_de_objeto(texto)
    return "INFRAESTRUTURA" if setor == "OUTRO" else setor


def fetch_page(sess: requests.Session, *, data_cadastro: date, page: int, size: int) -> dict:
    params = {
        "dataCadastro": data_cadastro.isoformat(),
        "natureza": "Obra",
        "pagina": page,
        "tamanhoDaPagina": size,
    }
    for tentativa in range(1, MAX_RETRIES + 1):
        r = sess.get(BASE_URL, params=params, timeout=60)
        if r.status_code == 429:
            espera = RATE_LIMIT_SLEEP_S * tentativa
            log.warning(
                "ObrasGov 429 data=%s page=%s tentativa=%s/%s; aguardando %ss",
                data_cadastro, page, tentativa, MAX_RETRIES, espera,
            )
            time.sleep(espera)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate-limit persistente em {data_cadastro} page={page}")


def iter_records(start: date, end: date, page_size: int, max_pages_per_day: int) -> Iterator[dict]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": "WiNSHubComercial/1.0 dados-abertos"})
    for dia in iter_dates(start, end):
        page = 0
        while page < max_pages_per_day:
            try:
                payload = fetch_page(sess, data_cadastro=dia, page=page, size=page_size)
            except Exception as exc:
                log.warning("ObrasGov falhou data=%s page=%s: %s", dia, page, exc)
                _STATS["erros"] += 1
                break
            content = payload.get("content") or []
            if not content:
                break
            for rec in content:
                yield rec
            if payload.get("last") is True:
                break
            page += 1
            time.sleep(REQUEST_SLEEP_S)
        time.sleep(REQUEST_SLEEP_S)


def record_para_obra(rec: Dict[str, Any], valor: float) -> Dict[str, Any]:
    executor_nome, executor_cnpj = primeiro_nome_codigo(rec.get("executores") or [])
    tomador_nome, tomador_cnpj = primeiro_nome_codigo(rec.get("tomadores") or [])
    empresa = executor_nome or tomador_nome
    cnpj = re.sub(r"\D", "", executor_cnpj or tomador_cnpj or "")
    if len(cnpj) != 14:
        cnpj = None
    nome = (rec.get("nome") or rec.get("descricao") or f"ObrasGov {rec.get('idUnico')}")[:220]
    descricao = "\n".join(
        x for x in [
            rec.get("descricao") or "",
            rec.get("metaGlobal") or "",
            f"Situacao ObrasGov: {rec.get('situacao') or ''}",
        ] if x
    )[:1500]
    return {
        "id_externo": f"OBRASGOV:{rec.get('idUnico')}",
        "nome": nome,
        "empresa": empresa,
        "cnpj": cnpj,
        "uf": rec.get("uf"),
        "municipio": None,
        "setor": setor_obrasgov(rec),
        "valor_estimado": valor,
        "fase": fase_de_situacao(rec.get("situacao"), rec.get("dataInicialEfetiva")),
        "fonte": FONTE,
        "url_fonte": "https://obrasgov.sistema.gov.br/",
        "data_anuncio": rec.get("dataCadastro"),
        "descricao": descricao,
    }


def inserir(conn, dados: Dict[str, Any], dry: bool) -> Optional[str]:
    if dry:
        return "dry-run"
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
                %s, 'anunciado', %s, 0.92,
                %s, false, %s
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
            """,
            (
                dados["nome"], dados["empresa"], dados["cnpj"],
                dados["uf"], dados["municipio"], dados["setor"],
                dados["valor_estimado"], dados["fase"], dados["fonte"],
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
    parser.add_argument("--since", type=parse_date, default=date.today() - timedelta(days=3))
    parser.add_argument("--until", type=parse_date, default=date.today())
    parser.add_argument("--valor-min", type=float, default=VALOR_MINIMO)
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--max-pages-per-day", type=int, default=20)
    args = parser.parse_args()

    inicio = args.since if args.backfill else date.today() - timedelta(days=3)
    fim = args.until if args.backfill else date.today()
    log.info("ObrasGov 100k — janela %s -> %s | valor >= R$ %.0f%s",
             inicio, fim, args.valor_min, " [DRY]" if args.dry else "")

    conn = None if args.dry else get_conn()
    amostra = []
    try:
        for rec in iter_records(inicio, fim, args.page_size, args.max_pages_per_day):
            _STATS["buscados"] += 1
            if (rec.get("natureza") or "").lower() != "obra":
                _STATS["filtrados_natureza"] += 1
                continue
            valor = valor_previsto(rec)
            if valor < args.valor_min:
                _STATS["filtrados_capex"] += 1
                continue
            dados = record_para_obra(rec, valor)
            if args.dry and len(amostra) < 10:
                amostra.append((dados["id_externo"], valor, dados["uf"], dados["nome"][:90]))
            try:
                oid = inserir(conn, dados, args.dry)
                if oid:
                    if oid == "dry-run":
                        _STATS["novos"] += 1
                    else:
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
        log.info("AMOSTRA %s | R$ %.0f | %s | %s", *item)
    log.info("ObrasGov 100k done — %s", _STATS)


if __name__ == "__main__":
    main()
