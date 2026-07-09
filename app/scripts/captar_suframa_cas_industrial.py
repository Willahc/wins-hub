#!/usr/bin/env python3
"""Captador SUFRAMA CAS - projetos industriais aprovados/em pauta.

Fonte oficial, sem chave:
https://www.gov.br/suframa/pt-br/composicao/cas/pauta/

Os PDFs de pauta do CAS trazem comunicacoes/proposicoes com processo,
interessado, CNPJ, tipo de projeto e produto. Nem sempre ha CAPEX individual;
quando o PDF nao traz valor por empresa, usamos o piso operacional de R$ 100 mil
para permitir entrada no funil sem inflar CAPEX.
"""
from __future__ import annotations

import argparse
import atexit
import io
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import pdfplumber
import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from _pncp_common import get_conn  # noqa: E402


FONTE = "suframa_cas_industrial"
BASE = "https://www.gov.br/suframa/pt-br/composicao/cas/pauta"
VALOR_MIN = 100_000.0
TIMEOUT = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_suframa_cas_industrial")

_STATS = {
    "buscados": 0,
    "novos": 0,
    "duplicados": 0,
    "erros": 0,
    "pdfs": 0,
    "filtrados_data": 0,
    "filtrados_sem_cnpj": 0,
}


@atexit.register
def _emit_stats() -> None:
    print(f"STATS_JSON: {json.dumps(_STATS)}", flush=True)


@dataclass
class Projeto:
    id_externo: str
    nome: str
    empresa: str
    cnpj: str
    processo: str
    tipo: str
    produto: str
    data_ref: date
    url: str
    descricao: str


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_cnpj(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def extract_pdf_text(content: bytes) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text(x_tolerance=1, y_tolerance=3) or "")
    return "\n".join(parts)


def discover_pdf_urls(years: Iterable[int], min_reuniao: int = 300, max_reuniao: int = 330) -> list[str]:
    urls: list[str] = []
    session = requests.Session()
    for year in years:
        for numero in range(max_reuniao, min_reuniao - 1, -1):
            url = f"{BASE}/{year}/cas-{numero}-pauta.pdf"
            try:
                r = session.get(url, timeout=20, stream=True)
                ctype = (r.headers.get("content-type") or "").lower()
                if r.status_code == 200 and ("pdf" in ctype or url.endswith(".pdf")):
                    urls.append(url)
            except Exception:
                continue
    return urls


def meeting_date(text: str, fallback_year: int) -> Optional[date]:
    m = re.search(r"A realizar-se no dia\s+(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})", text, re.I)
    meses = {
        "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
        "outubro": 10, "novembro": 11, "dezembro": 12,
    }
    if m:
        return date(int(m.group(3)), meses[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return date(fallback_year, 1, 1)


def split_blocks(text: str) -> list[str]:
    matches = list(re.finditer(r"\n(?:Comunica[cç][aã]o|Proposi[cç][aã]o)\s+\d+\s*\([^)]+\)", text, re.I))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append(text[start:end])
    return blocks


def extract_after_label(block: str, label: str) -> str:
    if label.upper() == "PROCESSO":
        m = re.search(r"\b(52710\.[0-9]{6}/[0-9]{4}-[0-9]{2})\b", block, re.I)
        if m:
            return norm_space(m.group(1))
    lines = [ln.strip() for ln in block.splitlines()]
    for idx, line in enumerate(lines):
        if line.upper() == label.upper():
            vals = []
            for nxt in lines[idx + 1: idx + 5]:
                if not nxt or nxt.upper() in {"PROCESSO", "INTERESSADO", "ASSUNTO"}:
                    continue
                vals.append(nxt)
                if label.upper() == "PROCESSO":
                    break
                if "CNPJ" in nxt.upper() or len(" ".join(vals)) > 25:
                    break
            return norm_space(" ".join(vals))
    return ""


def extract_empresa(block: str, fallback: str, cnpj: str) -> str:
    cnpj_fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    variants = [re.escape(cnpj_fmt), re.escape(cnpj)]
    pat = r"([A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇÜÀ&.,'’ºª\-/\s]{5,180}?),?\s*CNPJ(?:\s*(?:sob o n[ºo]|n[ºo]|:))?\s*[:.]?\s*(?:%s)" % "|".join(variants)
    m = re.search(pat, block, re.I)
    if m:
        empresa = norm_space(m.group(1))
        empresa = re.sub(r"^.*?\bempresa\s+", "", empresa, flags=re.I)
        empresa = re.sub(r"^.*?\bInteressado\s+", "", empresa, flags=re.I)
        empresa = re.sub(r"^.*\b52710\.[0-9]{6}/[0-9]{4}-[0-9]{2}\s+", "", empresa, flags=re.I)
        empresa = re.sub(r"^(?:Assunto|Processo)\s+", "", empresa, flags=re.I)
        parts = [p.strip(" ,.") for p in re.split(r"\n| {2,}", empresa) if p.strip(" ,.")]
        empresa = parts[-1] if parts else empresa
        if len(empresa) >= 5:
            return empresa[:300]
    empresa = re.sub(r",?\s*CNPJ.*$", "", fallback or "", flags=re.I).strip(" ,.")
    return empresa[:300] if empresa else f"CNPJ {cnpj}"


def extract_tipo(block: str) -> str:
    up = block.upper()
    for tipo in ("IMPLANTAÇÃO", "IMPLANTACAO", "AMPLIAÇÃO", "AMPLIACAO", "DIVERSIFICAÇÃO", "DIVERSIFICACAO", "ATUALIZAÇÃO", "ATUALIZACAO"):
        if tipo in up:
            return tipo
    return "PROJETO INDUSTRIAL"


def extract_produto(block: str) -> str:
    patterns = [
        r"para produ[cç][aã]o de\s+(.{20,280}?)(?:,\s*c[oó]digo| recebendo| nos termos|\.|\n\n)",
        r"para a atividade de\s+(.{10,180}?)(?:\.|\n\n|Necessidade)",
    ]
    for pat in patterns:
        m = re.search(pat, block, re.I | re.S)
        if m:
            produto = norm_space(m.group(1))
            produto = re.sub(r"\bAssunto\b", "", produto, flags=re.I)
            return norm_space(produto)
    return "Projeto tecnico-economico industrial"


def parse_projects(text: str, url: str, since: date, until: date) -> list[Projeto]:
    year_m = re.search(r"/pauta/(\d{4})/", url)
    fallback_year = int(year_m.group(1)) if year_m else date.today().year
    data_ref = meeting_date(text, fallback_year)
    if data_ref < since or data_ref > until:
        _STATS["filtrados_data"] += 1
        return []

    reuniao = re.search(r"(\d{3})[ªa]?\s+REUNI", text, re.I)
    reuniao_id = reuniao.group(1) if reuniao else re.sub(r"\D", "", url[-20:])
    out: list[Projeto] = []
    for idx, block in enumerate(split_blocks(text), 1):
        _STATS["buscados"] += 1
        if not re.search(r"projeto.*(?:industrial|servi[cç]o)|IMPLANTA|AMPLIA|DIVERSIFICA", block, re.I | re.S):
            continue
        cnpj_m = re.search(r"CNPJ(?:\s*(?:sob o n[ºo]|n[ºo]|:))?\s*[:.]?\s*([0-9.\-\/]{14,20})", block, re.I)
        cnpj = clean_cnpj(cnpj_m.group(1) if cnpj_m else "")
        if len(cnpj) != 14:
            _STATS["filtrados_sem_cnpj"] += 1
            continue
        processo = extract_after_label(block, "Processo")
        interessado = extract_after_label(block, "Interessado")
        if not interessado:
            m_emp = re.search(r"empresa\s+(.{5,120}?),\s*CNPJ", block, re.I | re.S)
            interessado = norm_space(m_emp.group(1) if m_emp else f"CNPJ {cnpj}")
        empresa = extract_empresa(block, interessado, cnpj)
        tipo = extract_tipo(block)
        produto = extract_produto(block)
        nome = f"SUFRAMA CAS {reuniao_id}: {tipo.title()} - {empresa}"[:220]
        descricao = norm_space(
            f"Processo SUFRAMA {processo}. Tipo: {tipo}. Produto/atividade: {produto}. "
            "Fonte: pauta oficial do Conselho de Administracao da SUFRAMA."
        )
        chave = processo or f"{reuniao_id}-{idx}-{cnpj}"
        out.append(Projeto(
            id_externo=f"SUFRAMA-CAS:{reuniao_id}:{chave}",
            nome=nome,
            empresa=empresa[:300],
            cnpj=cnpj,
            processo=processo,
            tipo=tipo,
            produto=produto[:500],
            data_ref=data_ref,
            url=url,
            descricao=descricao[:1500],
        ))
    return out


def inserir(conn, p: Projeto, dry: bool) -> Optional[str]:
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
                %s, %s, %s, %s, 'AM', 'Manaus', 'INDUSTRIAL',
                %s, '>= R$ 100 mil', 'APROVACAO_INCENTIVO', 'licenciado', 'SUFRAMA CAS',
                %s, 'OFICIAL', %s, %s, %s,
                0.82, %s, false,
                'PISO_CONSERVADOR_SUFRAMA', 72, 2, ARRAY['CIVIL_TECNICA','ELETRICA_INDUSTRIAL','ESTRUTURA_METALICA']
            )
            ON CONFLICT (id_externo) DO UPDATE SET
                nome = EXCLUDED.nome,
                empresa = NULLIF(EXCLUDED.empresa, ''),
                cnpj = EXCLUDED.cnpj,
                descricao = EXCLUDED.descricao,
                data_anuncio = EXCLUDED.data_anuncio,
                data_publicacao = EXCLUDED.data_publicacao
            RETURNING id
            """,
            (
                p.id_externo, p.nome, p.empresa, p.cnpj,
                VALOR_MIN, FONTE, p.url, p.data_ref, p.data_ref, p.descricao,
            ),
        )
        ret = cur.fetchone()
    conn.commit()
    return str(ret[0]) if ret else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--since", type=parse_date, default=date.today() - timedelta(days=45))
    parser.add_argument("--until", type=parse_date, default=date.today())
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--min-reuniao", type=int, default=300)
    parser.add_argument("--max-reuniao", type=int, default=330)
    args = parser.parse_args()

    years = range(args.since.year, args.until.year + 1)
    urls = discover_pdf_urls(years, args.min_reuniao, args.max_reuniao)
    log.info("SUFRAMA CAS industrial — %s -> %s | pdfs=%d%s",
             args.since, args.until, len(urls), " [DRY]" if args.dry else "")
    conn = None if args.dry else get_conn()
    try:
        for url in urls:
            try:
                r = requests.get(url, timeout=TIMEOUT)
                r.raise_for_status()
                _STATS["pdfs"] += 1
                projetos = parse_projects(extract_pdf_text(r.content), url, args.since, args.until)
                log.info("%s: projetos extraidos=%d", url.rsplit("/", 1)[-1], len(projetos))
                for p in projetos:
                    oid = inserir(conn, p, args.dry)
                    if oid:
                        _STATS["novos"] += 1
                        if args.dry and _STATS["novos"] <= 10:
                            log.info("AMOSTRA %s | %s | %s | %s", p.cnpj, p.empresa, p.tipo, p.produto[:80])
                    else:
                        _STATS["duplicados"] += 1
            except Exception as exc:
                log.warning("erro pdf %s: %s", url, exc)
                _STATS["erros"] += 1
                if conn:
                    conn.rollback()
    finally:
        if conn:
            conn.close()
    log.info("SUFRAMA CAS done — %s", _STATS)
    return 0 if _STATS["erros"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
