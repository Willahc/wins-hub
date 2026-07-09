#!/usr/bin/env python3
"""Captador Recife Licenciamento Urbanistico 100k.

Fonte CKAN publica, sem API key:
https://dados.recife.pe.gov.br/dataset/licenciamento-urbanistico

Como a base nao traz CAPEX, estima valor por area construida:
    valor_estimado = areatotalconstruida * R$ 2.500/m2

So insere processos urbanisticos de obra com valor estimado >= R$ 100 mil.
Idempotencia: id_externo = "RECIFE-LIC:<num_processo>".
"""
from __future__ import annotations

import argparse
import atexit
import csv
import io
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import get_conn  # noqa: E402


CSV_URL = (
    "https://dados.recife.pe.gov.br/dataset/f79dbcdf-ec99-4f4c-9b84-b33175c35528/"
    "resource/39927a3d-3235-436a-9dc8-df7f4ea3b720/download/licenciamento_urbanistico.csv"
)
FONTE = "recife_licenciamento_100k"
VALOR_M2 = 2500.0
VALOR_MIN = 100_000.0

KW_OBRA = re.compile(
    r"(ALVARA.*CONSTRU|APROVACAO.*PROJ|APROV\\..*PROJ|REFORMA|LEGALIZACAO|"
    r"HABITE-SE|DEMOLICAO|OBRA)",
    re.IGNORECASE,
)
STATUS_OK = {"DEFERIDO", "SOLICITADA", "EM EXIGÊNCIA", "EM TRAMITAÇÃO"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_recife_licenciamento_100k")

_STATS = {
    "buscados": 0,
    "novos": 0,
    "erros": 0,
    "filtrados_data": 0,
    "filtrados_status": 0,
    "filtrados_keyword": 0,
    "filtrados_capex": 0,
    "duplicados": 0,
}


@atexit.register
def _emit_stats() -> None:
    print(f"STATS_JSON: {json.dumps(_STATS)}", flush=True)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_br_float(value: str | None) -> Optional[float]:
    if not value:
        return None
    v = str(value).strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def parse_row_date(row: Dict[str, str]) -> Optional[date]:
    for key in ("data_entrada", "data_emissao_licenca", "data_conclusao"):
        val = (row.get(key) or "").strip()
        if not val:
            continue
        try:
            return parse_date(val[:10])
        except Exception:
            continue
    return None


def normalized_row(row: Dict[str, str]) -> Dict[str, str]:
    out = {}
    for k, v in row.items():
        kk = k.replace("\ufeff", "").strip()
        out[kk] = v
    return out


def passa(row: Dict[str, str], since: date, until: date, valor_min: float) -> tuple[bool, float, Optional[date]]:
    _STATS["buscados"] += 1
    data_ref = parse_row_date(row)
    if not data_ref or data_ref < since or data_ref > until:
        _STATS["filtrados_data"] += 1
        return False, 0.0, data_ref
    if (row.get("situacao_processo") or "").strip().upper() not in STATUS_OK:
        _STATS["filtrados_status"] += 1
        return False, 0.0, data_ref
    assunto = row.get("assunto") or row.get("tipo_processo") or ""
    if not KW_OBRA.search(assunto):
        _STATS["filtrados_keyword"] += 1
        return False, 0.0, data_ref
    area = parse_br_float(row.get("areatotalconstruida"))
    if not area or area <= 0:
        _STATS["filtrados_capex"] += 1
        return False, 0.0, data_ref
    valor = area * VALOR_M2
    if valor < valor_min:
        _STATS["filtrados_capex"] += 1
        return False, valor, data_ref
    return True, valor, data_ref


def inserir(conn, row: Dict[str, str], valor: float, data_ref: date, dry: bool) -> Optional[str]:
    num = (row.get("num_processo") or "").strip()
    if not num:
        return None
    area = parse_br_float(row.get("areatotalconstruida")) or 0
    assunto = (row.get("assunto") or row.get("tipo_processo") or "Licenciamento urbanistico").strip()
    endereco = (row.get("endereco_empreendimento") or "").strip()
    empresa = (row.get("razao_social") or row.get("razao_social_mercantil") or "").strip() or None
    cnpj = re.sub(r"\D", "", row.get("cnpj") or "")
    if len(cnpj) != 14:
        cnpj = None
    bairro = (row.get("bairro") or "").strip()
    nome = f"{assunto} — {bairro or 'Recife/PE'}"[:220]
    descricao = (
        f"{assunto}. Area construida informada: {area:.2f} m2. "
        f"Endereco: {endereco}. Situacao: {row.get('situacao_processo') or ''}. "
        f"Estimativa WiNS: area x R$ {VALOR_M2:.0f}/m2."
    )
    if dry:
        return "dry-run"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica, id_externo,
                capex_fonte
            ) VALUES (
                %s, %s, %s, 'PE', 'Recife', 'INFRAESTRUTURA',
                %s, 'LICENCIAMENTO', %s, 'OFICIAL',
                %s, 'licenciado', %s, 0.8,
                %s, false, %s,
                'ESTIMATIVA_AREA_M2'
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
            """,
            (
                nome, empresa, cnpj, valor, FONTE, CSV_URL,
                data_ref, descricao[:1500], f"RECIFE-LIC:{num}",
            ),
        )
        ret = cur.fetchone()
    conn.commit()
    return str(ret[0]) if ret else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--since", type=parse_date, default=date.today() - timedelta(days=7))
    parser.add_argument("--until", type=parse_date, default=date.today())
    parser.add_argument("--valor-min", type=float, default=VALOR_MIN)
    args = parser.parse_args()

    log.info("Recife licenciamento 100k — janela %s -> %s%s",
             args.since, args.until, " [DRY]" if args.dry else "")
    r = requests.get(CSV_URL, timeout=120)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    conn = None if args.dry else get_conn()
    amostra = []
    try:
        for raw in reader:
            row = normalized_row(raw)
            ok, valor, data_ref = passa(row, args.since, args.until, args.valor_min)
            if not ok or data_ref is None:
                continue
            if args.dry and len(amostra) < 10:
                amostra.append((row.get("num_processo"), valor, data_ref, row.get("assunto"), row.get("bairro")))
            try:
                oid = inserir(conn, row, valor, data_ref, args.dry)
                if oid:
                    if oid == "dry-run":
                        _STATS["novos"] += 1
                    else:
                        _STATS["novos"] += 1
                else:
                    _STATS["duplicados"] += 1
            except Exception as exc:
                log.warning("insert erro processo=%s: %s", row.get("num_processo"), exc)
                _STATS["erros"] += 1
                if conn:
                    conn.rollback()
    finally:
        if conn:
            conn.close()

    for item in amostra:
        log.info("AMOSTRA proc=%s | R$ %.0f | %s | %s | %s", *item)
    log.info("Recife licenciamento done — %s", _STATS)


if __name__ == "__main__":
    main()
