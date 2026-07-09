#!/usr/bin/env python3
"""Captador GeoSampa - HIS/HMP Alvará de Execucao 100k.

Fonte oficial WFS da Prefeitura de Sao Paulo:
https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/wfs

Camada:
geoportal:habitacao_popular

Essa camada traz area construida e dados de deferimento/autuacao. O valor eh
estimado por area x R$ 2.500/m2, com piso minimo de R$ 100 mil.
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import get_conn  # noqa: E402


WFS_URL = "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/wfs"
METADATA_URL = "https://metadados.geosampa.prefeitura.sp.gov.br/geonetwork/intranet/api/records/9763f0c0-1298-433c-9fd9-6160b9d71668"
LAYER = "geoportal:habitacao_popular"
FONTE = "geosampa_habitacao_popular_100k"
VALOR_M2 = 2500.0
VALOR_MIN = 100_000.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_geosampa_habitacao_popular_100k")

_STATS = {"buscados": 0, "novos": 0, "duplicados": 0, "erros": 0, "filtrados_data": 0}


@atexit.register
def _emit_stats() -> None:
    print(f"STATS_JSON: {json.dumps(_STATS)}", flush=True)


def parse_date(value: str | None) -> Optional[date]:
    if not value:
        return None
    v = str(value).strip()
    if not v or v == "***":
        return None
    v = v.replace("Z", "")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v[:19], fmt).date()
        except Exception:
            continue
    return None


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def pass_row(props: Dict[str, Any], since: date, until: date) -> tuple[bool, Optional[date], float]:
    _STATS["buscados"] += 1
    data = parse_date(props.get("dt_deferimento_execucao")) or parse_date(props.get("dt_autuacao_execucao")) or parse_date(props.get("dt_atualizacao"))
    if not data or data < since or data > until:
        _STATS["filtrados_data"] += 1
        return False, data, 0.0
    area = props.get("qt_area_construida_total")
    try:
        area_f = float(area) if area is not None else 0.0
    except Exception:
        area_f = 0.0
    valor = area_f * VALOR_M2
    if valor < VALOR_MIN:
        return False, data, valor
    return True, data, valor


def inserir(conn, props: Dict[str, Any], data_ref: date, valor: float, dry: bool) -> Optional[str]:
    doc = norm(props.get("cd_numero_documento_execucao"))
    proc = norm(props.get("cd_numero_processo_execucao"))
    ident = doc or proc or norm(props.get("cd_identificador_habitacao_popular"))
    if not ident:
        return None
    id_externo = f"GEOSAMPA-HISHMP:{ident}"
    area_terreno = props.get("qt_area_total_terreno")
    area_const = props.get("qt_area_construida_total")
    assunto = norm(props.get("tx_assunto_alvara")) or "Alvara de Execucao"
    endereco = norm(props.get("tx_grupo_endereco"))
    qt_unid = props.get("qt_unidade_his") or 0
    qtd_hmp = props.get("qt_unidade_hmp") or 0
    nome = f"GeoSampa HIS/HMP - {assunto} - {endereco}"[:220]
    descricao = (
        f"Assunto: {assunto}. Processo: {proc}. Documento: {doc}. "
        f"Area construida total: {area_const}. Area terreno: {area_terreno}. "
        f"Unidades HIS: {props.get('qt_unidade_his_tipo_1') or 0}+{props.get('qt_unidade_his_tipo_2') or 0}. "
        f"HMP: {qtd_hmp}. Fonte: camada oficial HIS/HMP do GeoSampa."
    )
    if dry:
        return "dry-run"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO obras (
                id_externo, nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, valor_formatado, fase, status, status_licenca,
                fonte, fonte_tipo, url_fonte, data_anuncio, data_publicacao,
                confianca_extracao, descricao, descricao_sintetica,
                capex_fonte, lead_score, urgencia, necessidades
            ) VALUES (
                %s, %s, NULL, NULL, 'SP', 'Sao Paulo', 'INFRAESTRUTURA',
                %s, '>= R$ 100 mil', 'LICENCIAMENTO', 'licenciado', 'ALVARA',
                %s, 'OFICIAL', %s, %s, %s,
                0.85, %s, false,
                'ESTIMATIVA_AREA_M2_GEOSAMPA', 75, 2, ARRAY['CIVIL_TECNICA','ELETRICA_INDUSTRIAL','HIDRAULICA']
            )
            ON CONFLICT (id_externo) DO UPDATE SET
                nome = EXCLUDED.nome,
                valor_estimado = EXCLUDED.valor_estimado,
                valor_formatado = EXCLUDED.valor_formatado,
                descricao = EXCLUDED.descricao,
                data_anuncio = EXCLUDED.data_anuncio,
                data_publicacao = EXCLUDED.data_publicacao
            RETURNING id
            """,
            (
                id_externo,
                nome,
                valor,
                FONTE,
                METADATA_URL,
                data_ref,
                data_ref,
                descricao[:1500],
            ),
        )
        ret = cur.fetchone()
    conn.commit()
    return str(ret[0]) if ret else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=date.today() - timedelta(days=365))
    parser.add_argument("--until", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=date.today())
    args = parser.parse_args()

    log.info("GeoSampa HIS/HMP 100k — %s -> %s%s", args.since, args.until, " [DRY]" if args.dry else "")
    r = requests.get(
        WFS_URL,
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": LAYER,
            "count": 6000,
            "outputFormat": "application/json",
        },
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    features = data.get("features", [])
    conn = None if args.dry else get_conn()
    try:
        for feat in features:
            props = feat.get("properties") or {}
            ok, data_ref, valor = pass_row(props, args.since, args.until)
            if not ok or not data_ref:
                continue
            try:
                oid = inserir(conn, props, data_ref, valor, args.dry)
                if oid:
                    _STATS["novos"] += 1
                    if args.dry and _STATS["novos"] <= 10:
                        log.info(
                            "AMOSTRA %s | %s | R$ %.0f | area=%.2f",
                            props.get("cd_numero_documento_execucao") or props.get("cd_numero_processo_execucao"),
                            norm(props.get("tx_assunto_alvara")),
                            valor,
                            float(props.get("qt_area_construida_total") or 0),
                        )
                else:
                    _STATS["duplicados"] += 1
            except Exception as exc:
                log.warning("erro feature=%s: %s", props.get("cd_numero_documento_execucao"), exc)
                _STATS["erros"] += 1
                if conn:
                    conn.rollback()
    finally:
        if conn:
            conn.close()

    log.info("GeoSampa done — %s", _STATS)
    return 0 if _STATS["erros"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
