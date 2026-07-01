#!/usr/bin/env python3
"""Captador Eletrobras / Axia Energia — releases de RI.

Eletrobras foi rebrand-ada pra Axia Energia em 2026 (ri.axia.com.br).
ri.eletrobras.com retorna 403 anti-bot. Solucao: Playwright headless.

Fluxo:
  1. Playwright abre https://ri.axia.com.br/informacoes-ao-mercado/...
  2. Coleta links pra "Fato Relevante" e "Comunicado ao Mercado"
  3. Para cada link (PDF em api.mziq.com), baixa via requests, extrai texto
     via pdfplumber.
  4. Filtra texto por keywords (CAPEX, investimento, R$ X milhoes/bilhoes, obra).
  5. Haiku 4.5 extrai dados estruturados.
  6. INSERT em obras com fonte='eletrobras_ri', id_externo=hash da URL.

Idempotente, STATS_JSON na ultima linha.
"""
from __future__ import annotations

import argparse
import sys as _scompat
if "/app" not in _scompat.path: _scompat.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client, _haiku_async_client  # free-first 25/06
import atexit
import hashlib
import io
import json as _json
import logging
import os
import re
import sys
from datetime import date
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import psycopg2
import requests
from brutils import is_valid_cnpj


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_eletrobras_ri")

FONTE = "eletrobras_ri"
PORTAL_URL = "https://ri.axia.com.br/informacoes-ao-mercado/avisos-comunicados-e-fatos-relevantes-axia-energia/"
MAX_LINKS_POR_RUN = 20

KEYWORDS_OBRA = (
    r"\bobra\b", r"investiment[oa]", r"capex",
    r"r\$\s*\d[\d\.\,\s]*\s*(?:milh|bilh)",
    r"subesta(?:cao|coes)", r"linha\s+de\s+transmiss",
    r"contrato\s+de\s+(?:concessao|gestao|epc)",
    r"aquisic(?:ao|oes)\s+de\s+(?:ativos|empreendimento)",
)
KEYWORDS_OBRA_RE = re.compile("|".join(KEYWORDS_OBRA), re.IGNORECASE)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
PROMPT_AXIA = """Voce analisa Fatos Relevantes / Comunicados de RI da Axia Energia
(ex-Eletrobras), uma das maiores geradoras e transmissoras de energia do Brasil.

Extraia APENAS se o documento anuncia OBRA, INVESTIMENTO ou AQUISICAO com CAPEX
identificavel. Pule resultados financeiros sem CAPEX nominal, calls de
dividendos, sucessao de executivos.

TITULO: {titulo}

TEXTO ({n_chars} chars):
{texto}

Retorne JSON puro (sem markdown), schema:
{{
  "eh_obra_real": bool,
  "motivo_skip": "string se eh_obra_real=false, senao null",
  "empresa_nome": "Axia Energia ou subsidiaria especifica (Furnas, Chesf, etc)",
  "cnpj_provavel": "14 digitos ou null",
  "capex_brl": "valor em REAIS, numero puro. R$ 2 bi -> 2000000000",
  "uf": "sigla 2 letras se mencionado",
  "municipio": "string ou null",
  "setor": "ENERGIA (default pra Axia)",
  "tipo_publicacao": "FATO_RELEVANTE/COMUNICADO/RELEASE",
  "descricao_curta": "1 frase ate 200 chars",
  "confianca": "0.0-1.0"
}}

Se confianca < 0.6 marque eh_obra_real=false."""

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

_STATS = {
    "buscados": 0,
    "novos": 0,
    "erros": 0,
    "filtrados_keyword": 0,
    "extracoes_haiku": 0,
    "haiku_skip": 0,
}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def coletar_links_portal(limite: int = MAX_LINKS_POR_RUN) -> List[Dict[str, str]]:
    """Playwright headless coleta links de fato relevante / comunicado / release."""
    from playwright.sync_api import sync_playwright

    out: List[Dict[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
        )
        page = ctx.new_page()
        try:
            page.goto(PORTAL_URL, timeout=30000, wait_until="networkidle")
        except Exception as e:
            log.error(f"playwright goto falhou: {e}")
            browser.close()
            return []
        # Cada item geralmente tem titulo + link pro PDF
        items = page.eval_on_selector_all(
            "a",
            "els => els.map(e => ({"
            "  h: e.href, "
            "  t: (e.innerText || '').trim()"
            "})).filter(o => o.h && o.t && "
            "  /fato relevante|comunicado|release|aviso/i.test(o.t))",
        )
        for it in items[: limite * 2]:  # pode ter duplicatas/links secundarios
            if not it.get("h") or not it.get("t"):
                continue
            out.append({"titulo": it["t"][:200], "url": it["h"]})
        browser.close()
    # Dedup por URL
    seen = set()
    uniq = []
    for o in out:
        if o["url"] in seen:
            continue
        seen.add(o["url"])
        uniq.append(o)
    return uniq[:limite]


def baixar_pdf_texto(url: str, sess: Optional[requests.Session] = None) -> Optional[str]:
    """Download PDF + extrai texto via pdfplumber."""
    import pdfplumber

    sess = sess or requests.Session()
    try:
        r = sess.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        log.warning(f"PDF download falhou {url}: {e}")
        return None
    if "pdf" not in (r.headers.get("content-type", "").lower()) and not r.content.startswith(b"%PDF"):
        log.debug(f"  not a PDF: {url}")
        return None
    try:
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            partes = [pg.extract_text() or "" for pg in pdf.pages]
        return "\n".join(partes)
    except Exception as e:
        log.warning(f"PDF parse falhou {url}: {e}")
        return None


def haiku_extrair(client, titulo: str, texto: str) -> Optional[Dict[str, Any]]:
    prompt = PROMPT_AXIA.format(titulo=titulo[:300],
                                texto=texto[:6000],
                                n_chars=len(texto))
    try:
        r = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning(f"haiku falhou: {e}")
        return None
    raw = (r.content[0].text if r.content else "").strip()
    raw = re.sub(r"^```(?:json)?", "", raw)
    raw = re.sub(r"```\s*$", "", raw).strip()
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        log.debug(f"haiku JSON parse falhou: {raw[:200]}")
        return None


def url_to_id_ext(url: str) -> str:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return f"AXIA:{h}"


def inserir_obra(conn, url: str, titulo: str, dados: Dict[str, Any]) -> Optional[str]:
    cnpj_raw = (dados.get("cnpj_provavel") or "").strip()
    cnpj_clean = re.sub(r"\D", "", cnpj_raw)
    cnpj_valido = is_valid_cnpj(cnpj_clean) if len(cnpj_clean) == 14 else False

    nome = (dados.get("descricao_curta") or titulo or "Axia Energia release")[:200]
    empresa = (dados.get("empresa_nome") or "Axia Energia")[:255]
    uf = (dados.get("uf") or "")[:2] or None
    municipio = dados.get("municipio")
    capex = dados.get("capex_brl")
    if isinstance(capex, str):
        try:
            capex = float(re.sub(r"[^\d\.]", "", capex.replace(",", ".")))
        except ValueError:
            capex = None
    setor = "ENERGIA"
    confianca = float(dados.get("confianca") or 0.7)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica, id_externo
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, 'LICITACAO_ABERTA', %s, 'OFICIAL',
                %s, 'anunciado', %s, %s,
                %s, false, %s
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
        """, (
            nome, empresa, cnpj_clean if cnpj_valido else None,
            uf, municipio, setor, capex, FONTE,
            url, date.today(), confianca,
            titulo, url_to_id_ext(url),
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limite", type=int, default=MAX_LINKS_POR_RUN)
    args = parser.parse_args()

    log.info(f"Eletrobras/Axia RI — coletando ate {args.limite} links via Playwright")

    links = coletar_links_portal(limite=args.limite)
    log.info(f"  links coletados: {len(links)}")

    from anthropic import Anthropic
    client = _haiku_client(api_key=os.getenv("ANTHROPIC_API_KEY"))

    conn = psycopg2.connect(**DB_CONFIG)
    sess = requests.Session()
    try:
        for link in links:
            _STATS["buscados"] += 1
            url = link["url"]
            titulo = link["titulo"]

            if "api.mziq.com" not in url and not url.lower().endswith(".pdf"):
                # nao e PDF — pular nesta v1
                continue

            texto = baixar_pdf_texto(url, sess=sess)
            if not texto:
                continue
            if not KEYWORDS_OBRA_RE.search(texto):
                continue
            _STATS["filtrados_keyword"] += 1

            dados = haiku_extrair(client, titulo, texto)
            _STATS["extracoes_haiku"] += 1
            if not dados or not dados.get("eh_obra_real"):
                _STATS["haiku_skip"] += 1
                continue

            try:
                oid = inserir_obra(conn, url, titulo, dados)
                if oid:
                    _STATS["novos"] += 1
            except Exception as e:
                log.warning(f"insert erro {url}: {e}")
                _STATS["erros"] += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        conn.close()

    log.info(f"Eletrobras RI done — {_STATS}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
