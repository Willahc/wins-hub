#!/usr/bin/env python3
"""Captador Curitiba - Base de Alvaras 100k.

Fonte publica oficial:
https://dadosabertos.curitiba.pr.gov.br/conjuntodado/detalhe/?chave=be211e1f-cff5-44cb-9aaa-1be6b9ec3811

O dataset traz alvaras ativos e eh atualizado mensalmente. Nao ha CAPEX
explicito, entao usamos um piso conservador de R$ 100 mil para licencas
relacionadas a obras/edificacoes/atividade industrial relevante.
"""
from __future__ import annotations

import argparse
import atexit
import csv
import io
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, Optional

import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import get_conn  # noqa: E402


DETAIL_URL = "https://dadosabertos.curitiba.pr.gov.br/conjuntodado/detalhe/?chave=be211e1f-cff5-44cb-9aaa-1be6b9ec3811"
DOWNLOAD_API = "https://dadosabertos.curitiba.pr.gov.br/ConjuntoDado/DownloadArquivos/"
STATE_PATH = "/app/logs/curitiba_alvaras_state.json"

FONTE = "curitiba_alvaras_100k"
VALOR_MIN = 100_000.0

KW_INFRA = (
    "CONSTRU", "EDIFICA", "OBRA", "REFORMA", "DEMOLI", "INCORPORA",
    "ENGENHARIA", "HABITA", "ALVENARIA", "ESTRUTURA", "MONTAGEM",
    "INSTALAC", "INSTALA", "PREDIAL",
)
KW_OBRA_ESPECIFICA = (
    "ALUGUEL DE MÁQUINAS PARA CONSTRU", "ALUGUEL DE MAQUINAS PARA CONSTRU",
    "ATIVIDADES RELACIONADAS A ESGOTO", "SANEAMENTO",
    "INSTALAÇÃO ELÉTRICA", "INSTALACAO ELETRICA",
    "INSTALAÇÕES HIDRÁULICAS", "INSTALACOES HIDRAULICAS",
    "OBRAS DE ALVENARIA", "MONTAGEM DE ESTRUTURAS METÁLICAS", "MONTAGEM DE ESTRUTURAS METALICAS",
    "SERVIÇOS DE ENGENHARIA", "SERVICOS DE ENGENHARIA",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_curitiba_alvaras_100k")

_STATS = {
    "buscados": 0,
    "novos": 0,
    "duplicados": 0,
    "erros": 0,
    "filtrados_data": 0,
    "filtrados_keyword": 0,
    "arquivo": None,
}


@atexit.register
def _emit_stats() -> None:
    print(f"STATS_JSON: {json.dumps(_STATS)}", flush=True)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%d/%m/%Y").date()


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def discover_latest_file() -> tuple[str, str]:
    r = requests.get(
        DOWNLOAD_API,
        params={
            "conjuntoDadoChave": "be211e1f-cff5-44cb-9aaa-1be6b9ec3811",
            "conjuntoDadoExtensao": "377f4e23-0e4f-4f11-954f-ae06ba689558",
            "pagina": 1,
            "tamanhoPagina": 1,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    html = data.get("tabela", "")
    m = re.search(r"href='(https://mid-dadosabertos\.curitiba\.pr\.gov\.br/BaseAlvaras/[^']+_Base_de_Dados\.csv)'", html)
    if not m:
        raise RuntimeError("nao foi possivel descobrir o arquivo mais recente")
    url = m.group(1)
    filename = url.rsplit("/", 1)[-1]
    return filename, url


def extract_sector(row: Dict[str, str]) -> tuple[str, list[str]]:
    text = " ".join(
        norm(row.get(k)) for k in ["ATIVIDADE_PRINCIPAL", "NOME_EMPRESARIAL", "NOME_FANTASIA"]
    ).upper()
    text += " " + " ".join(
        norm(row.get(f"ATIVIDADE_SECUNDARIA{i:02d}")) for i in range(1, 11)
    ).upper()
    if any(k in text for k in KW_INFRA):
        return "INFRAESTRUTURA", ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL", "HIDRAULICA"]
    if any(k in text for k in KW_OBRA_ESPECIFICA):
        return "INFRAESTRUTURA", ["CIVIL_TECNICA", "ELETRICA_INDUSTRIAL"]
    return "", []


def passa(row: Dict[str, str], since: date, until: date) -> bool:
    _STATS["buscados"] += 1
    data = norm(row.get("DATA_EMISSAO"))
    if not data:
        _STATS["filtrados_data"] += 1
        return False
    try:
        dt = parse_date(data)
    except Exception:
        _STATS["filtrados_data"] += 1
        return False
    if dt < since or dt > until:
        _STATS["filtrados_data"] += 1
        return False
    setor, _ = extract_sector(row)
    if not setor:
        _STATS["filtrados_keyword"] += 1
        return False
    return True


def inserir(conn, row: Dict[str, str], setor: str, necessidades: list[str], dry: bool) -> Optional[str]:
    num_alvara = norm(row.get("NUMERO_DO_ALVARA"))
    if not num_alvara:
        return None
    nome_emp = norm(row.get("NOME_EMPRESARIAL")) or norm(row.get("NOME_FANTASIA")) or f"Alvara {num_alvara}"
    fantasia = norm(row.get("NOME_FANTASIA"))
    atividade = norm(row.get("ATIVIDADE_PRINCIPAL"))
    secundarias = [
        norm(row.get(f"ATIVIDADE_SECUNDARIA{i:02d}"))
        for i in range(1, 11)
        if norm(row.get(f"ATIVIDADE_SECUNDARIA{i:02d}"))
    ]
    endereco = " ".join(
        part for part in [
            norm(row.get("ENDERECO")),
            norm(row.get("NUMERO")),
            norm(row.get("UNIDADE")),
            norm(row.get("ANDAR")),
            norm(row.get("COMPLEMENTO")),
            norm(row.get("BAIRRO")),
            norm(row.get("CEP")),
        ] if part and part != "***"
    )
    data_emissao = parse_date(norm(row.get("DATA_EMISSAO")))
    id_externo = f"CURITIBA-ALVARA:{num_alvara}"
    descricao = (
        f"Alvara Curitiba. Atividade principal: {atividade}. "
        f"Atividades secundarias: {'; '.join(secundarias[:5])}. "
        f"Endereco: {endereco}. Fonte: Base de Alvaras do portal de dados abertos de Curitiba."
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
                %s, %s, %s, NULL, 'PR', 'Curitiba', %s,
                %s, '>= R$ 100 mil', 'LICENCIAMENTO', 'licenciado', 'ALVARA',
                %s, 'OFICIAL', %s, %s, %s,
                0.72, %s, false,
                'PISO_CONSERVADOR_ALVARA_CURITIBA', 68, 2, %s
            )
            ON CONFLICT (id_externo) DO UPDATE SET
                nome = EXCLUDED.nome,
                empresa = EXCLUDED.empresa,
                setor = EXCLUDED.setor,
                valor_estimado = EXCLUDED.valor_estimado,
                valor_formatado = EXCLUDED.valor_formatado,
                descricao = EXCLUDED.descricao,
                data_anuncio = EXCLUDED.data_anuncio,
                data_publicacao = EXCLUDED.data_publicacao,
                necessidades = EXCLUDED.necessidades
            RETURNING id
            """,
            (
                id_externo,
                f"Alvara Curitiba - {nome_emp}"[:220],
                fantasia or nome_emp,
                setor,
                VALOR_MIN,
                FONTE,
                DETAIL_URL,
                data_emissao,
                data_emissao,
                descricao[:1500],
                list(necessidades),
            ),
        )
        ret = cur.fetchone()
    conn.commit()
    return str(ret[0]) if ret else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--since", type=parse_date, default=date.today() - timedelta(days=365))
    parser.add_argument("--until", type=parse_date, default=date.today())
    parser.add_argument("--force", action="store_true", help="Processa mesmo se o arquivo mais recente for o mesmo")
    args = parser.parse_args()

    filename, url = discover_latest_file()
    _STATS["arquivo"] = filename
    state = load_state()
    if not args.force and state.get("arquivo") == filename and not args.dry:
        log.info("Curitiba alvaras sem novidade: %s", filename)
        return 0

    log.info("Curitiba alvaras 100k — arquivo=%s | %s -> %s%s", filename, args.since, args.until, " [DRY]" if args.dry else "")
    resp = requests.get(url, stream=True, timeout=180)
    resp.raise_for_status()

    conn = None if args.dry else get_conn()
    try:
        text_iter = (line.decode("latin-1", errors="replace") for line in resp.iter_lines())
        reader = csv.DictReader(text_iter, delimiter=";")
        for row in reader:
            if not passa(row, args.since, args.until):
                continue
            setor, necessidades = extract_sector(row)
            try:
                oid = inserir(conn, row, setor, necessidades, args.dry)
                if oid:
                    _STATS["novos"] += 1
                    if args.dry and _STATS["novos"] <= 10:
                        log.info(
                            "AMOSTRA %s | %s | %s | %s",
                            row.get("NUMERO_DO_ALVARA"),
                            norm(row.get("NOME_EMPRESARIAL")) or norm(row.get("NOME_FANTASIA")),
                            setor,
                            norm(row.get("ATIVIDADE_PRINCIPAL")),
                        )
                else:
                    _STATS["duplicados"] += 1
            except Exception as exc:
                log.warning("erro alvara=%s: %s", row.get("NUMERO_DO_ALVARA"), exc)
                _STATS["erros"] += 1
                if conn:
                    conn.rollback()
    finally:
        if conn:
            conn.close()

    if not args.dry:
        save_state({"arquivo": filename, "url": url, "ultima_execucao": datetime.utcnow().isoformat() + "Z"})

    log.info("Curitiba alvaras done — %s", _STATS)
    return 0 if _STATS["erros"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
