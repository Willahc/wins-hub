#!/usr/bin/env python3
"""Captador DOE/DOM via Querido-Diario API (OKBR).

NOTA SOBRE ESCOPO:
  Sprint dia 3 brief pediu "DOEs estaduais piloto SP/RJ/MG". Esses 3 portais
  estaduais têm anti-bot forte (Cloudflare/403) que requer trabalho de
  scraping per-estado. Pivotamos para Querido-Diario API (OKBR), que:
    - Cobre 5000+ municipalidades brasileiras (DOMs primarily).
    - Inclui texto pre-extraido por OKBR (txt_url) — sem pdfplumber por padrao.
    - API publica REST, sem auth (https://api.queridodiario.ok.org.br/gazettes).
    - Trabalho per-estado vira config YAML, nao codigo.

  Trade-off: cobertura municipal, nao estadual. Para DOEs estaduais (SP/RJ/MG/etc)
  fica trabalho de sessao dedicada por estado.

API:
  GET /gazettes?querystring=<kw>&size=N
  Retorna {"total_gazettes": N, "gazettes": [{territory_id, date, state_code,
    territory_name, url (PDF), txt_url, excerpts, ...}]}
  Filter de date no client (API ignora since/until em alguns endpoints).

Fluxo:
  1. Le YAML com territory_ids ou state_codes alvo.
  2. Para cada keyword em KEYWORDS_BUSCA, GET top N gazettes.
  3. Filtra por date >= today - DIAS_BACK.
  4. Filtra por territory_id/state_code em allowlist.
  5. Fetch txt_url. Regex keyword filter LOCAL pra reduzir Haiku calls.
  6. Haiku extrai dados estruturados.
  7. INSERT obras fonte='doe_<state_code>' fonte_tipo='OFICIAL'.
  8. Idempotente via id_externo = "QD:<sha1(url)[:16]>".

STATS_JSON na ultima linha.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json as _json
import logging
import os
import re
import sys
import traceback as _tb
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import psycopg2
import requests
import yaml
from brutils import is_valid_cnpj


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_doe")

API_BASE = "https://api.queridodiario.ok.org.br/gazettes"
KEYWORDS_BUSCA = ("obra", "licenca", "construc", "edital", "pavimentacao", "outorga")
DEFAULT_DIAS_BACK = 7
DEFAULT_SIZE_POR_KW = 25
DEFAULT_CONFIG_YAML = "/app/scripts/doe_config.yaml"

KEYWORDS_OBRA_LOCAL = (
    r"\bobra\b", r"construc(?:ao|oes)\s+de", r"reform(?:a|as)\s+(?:de|do|da)",
    r"amplia(?:cao|coes)", r"implanta(?:cao|coes)", r"edifica(?:cao|coes)",
    r"licenc(?:a|as)\s+(?:ambiental|de\s+instalac|de\s+operac|previa)",
    r"investiment[oa]", r"capex",
    r"r\$\s*\d[\d\.\,\s]*\s*(?:milh|bilh)",
    r"pavimenta(?:cao|coes)", r"subesta(?:cao|coes)", r"linha\s+de\s+transmiss",
    r"rodovia", r"ferrovia", r"porto", r"aeroporto",
    r"hospital", r"unidade\s+basica", r"ubs", r"upa",
    r"escola", r"creche",
    r"outorga", r"concessao",
    r"adutora", r"esgoto", r"saneamento",
)
KEYWORDS_OBRA_RE = re.compile("|".join(KEYWORDS_OBRA_LOCAL), re.IGNORECASE)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
PROMPT_DOE = """Voce analisa publicacoes de Diarios Oficiais municipais/estaduais
brasileiros (DOM/DOE). Extraia APENAS se a publicacao anuncia OBRA/INVESTIMENTO
REAL (nao retificacao, nao prorrogacao de prazo sem CAPEX, nao decreto puramente
formal).

MUNICIPIO/UF: {territory} ({state})
DATA: {data}
EXCERPTS (trechos com keyword):
{excerpts}

TEXTO (primeiros chars):
{texto}

Retorne JSON puro (sem markdown), schema:
{{
  "eh_obra_real": bool,
  "motivo_skip": "string ou null",
  "empresa_nome": "razao social ou orgao licitante",
  "cnpj_provavel": "14 digitos ou null",
  "capex_brl": "valor em REAIS numero puro. 'R$ 2 mi' -> 2000000",
  "uf": "sigla 2 letras",
  "municipio": "string ou null",
  "setor": "EXATAMENTE: INDUSTRIAL, ENERGIA, LOGISTICO, MINERACAO, INFRAESTRUTURA, SANEAMENTO, AGRO, DATA_CENTER, OUTRO",
  "tipo_publicacao": "EDITAL/ORDEM_INICIO/CONTRATO/LICENCA/DECRETO/OUTRO",
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
    "fetched_text": 0,
    "filtrados_keyword": 0,
    "extracoes_haiku": 0,
    "haiku_skip": 0,
}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def carregar_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        log.warning(f"config nao encontrada em {path}; usando defaults")
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def buscar_gazettes_por_keyword(keyword: str, size: int = DEFAULT_SIZE_POR_KW,
                                sess: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    sess = sess or requests.Session()
    try:
        r = sess.get(API_BASE, params={"querystring": keyword, "size": size},
                     timeout=30, headers={"User-Agent": "WiNS-Hub captar_doe/1.0"})
        r.raise_for_status()
    except Exception as e:
        log.warning(f"API fetch keyword={keyword!r}: {e}")
        return []
    try:
        return r.json().get("gazettes", [])
    except Exception:
        return []


def fetch_texto(txt_url: Optional[str], url_pdf: Optional[str],
                sess: Optional[requests.Session] = None) -> str:
    sess = sess or requests.Session()
    if txt_url:
        try:
            r = sess.get(txt_url, timeout=60, headers={"User-Agent": "WiNS-Hub"})
            if r.status_code == 200 and r.text:
                return r.text
        except Exception as e:
            log.debug(f"txt fetch falhou: {e}")
    # fallback: baixar PDF e extrair via pdfplumber
    if url_pdf:
        try:
            import io
            import pdfplumber
            r = sess.get(url_pdf, timeout=60, headers={"User-Agent": "WiNS-Hub"})
            r.raise_for_status()
            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            log.debug(f"pdf fetch falhou: {e}")
    return ""


def haiku_extrair(client, gazette: Dict[str, Any], texto: str) -> Optional[Dict[str, Any]]:
    excerpts = "\n".join(gazette.get("excerpts") or [])[:1500]
    prompt = PROMPT_DOE.format(
        territory=gazette.get("territory_name", "?"),
        state=gazette.get("state_code", "??"),
        data=gazette.get("date", "?"),
        excerpts=excerpts,
        texto=(texto or "")[:5000],
    )
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
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        return None
    # defensivo: Haiku as vezes envolve em lista
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalizar_setor(s: Optional[str]) -> str:
    if not s:
        return "OUTRO"
    s = s.strip().upper()
    canon = {"INDUSTRIAL", "ENERGIA", "LOGISTICO", "MINERACAO", "INFRAESTRUTURA",
             "SANEAMENTO", "AGRO", "DATA_CENTER", "OUTRO"}
    if s in canon:
        return s
    aliases = {"INDUSTRIA": "INDUSTRIAL", "LOGISTICA": "LOGISTICO",
               "TECNOLOGIA": "DATA_CENTER", "GOVERNO": "OUTRO"}
    return aliases.get(s, "OUTRO")


def url_to_id_ext(url: str) -> str:
    return "QD:" + hashlib.sha1((url or "").encode()).hexdigest()[:16]


def inserir_obra(conn, gazette: Dict[str, Any], dados: Dict[str, Any]) -> Optional[str]:
    cnpj_raw = (dados.get("cnpj_provavel") or "").strip()
    cnpj_clean = re.sub(r"\D", "", cnpj_raw)
    cnpj_valido = is_valid_cnpj(cnpj_clean) if len(cnpj_clean) == 14 else False

    state = (gazette.get("state_code") or "")[:2] or None
    municipio = gazette.get("territory_name")
    nome = (dados.get("descricao_curta")
            or f"{gazette.get('territory_name', '?')} {gazette.get('date', '')}").strip()[:200]
    empresa = (dados.get("empresa_nome") or "")[:255]
    capex = dados.get("capex_brl")
    if isinstance(capex, str):
        try:
            capex = float(re.sub(r"[^\d\.]", "", capex.replace(",", ".")))
        except ValueError:
            capex = None
    setor = _normalizar_setor(dados.get("setor"))
    confianca = float(dados.get("confianca") or 0.6)
    fonte_uf = f"doe_{(state or 'br').lower()}"
    try:
        data_anuncio = datetime.strptime(gazette.get("date", ""), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        data_anuncio = None

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
            (dados.get("uf") or state or "")[:2] or None,
            dados.get("municipio") or municipio,
            setor, capex, fonte_uf,
            gazette.get("url") or "", data_anuncio, confianca,
            (gazette.get("excerpts", [""])[0] or "")[:1000],
            url_to_id_ext(gazette.get("url") or ""),
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG_YAML)
    parser.add_argument("--dias-back", type=int, default=DEFAULT_DIAS_BACK)
    parser.add_argument("--max-por-keyword", type=int, default=DEFAULT_SIZE_POR_KW)
    parser.add_argument("--state-codes", default="",
                        help="filtro UF csv (ex: ES,CE,SE). Vazio = config YAML.")
    args = parser.parse_args()

    config = carregar_config(args.config)
    state_filter_csv = args.state_codes or config.get("state_codes", "")
    state_filter = {s.strip().upper() for s in state_filter_csv.split(",") if s.strip()}
    territory_filter = set(config.get("territory_ids") or [])

    if not state_filter and not territory_filter:
        log.warning("Nem state_codes nem territory_ids configurados — abortando")
        return 2

    data_min = date.today() - timedelta(days=args.dias_back)
    log.info(f"DOE/DOM via Querido-Diario — states={sorted(state_filter)} "
             f"territories={len(territory_filter)} dias_back={args.dias_back}")

    sess = requests.Session()
    gazettes_unicos: Dict[str, Dict[str, Any]] = {}
    for kw in KEYWORDS_BUSCA:
        for g in buscar_gazettes_por_keyword(kw, size=args.max_por_keyword, sess=sess):
            key = g.get("url") or f"{g.get('territory_id')}:{g.get('date')}"
            if key in gazettes_unicos:
                continue
            # filter date
            try:
                d = datetime.strptime(g.get("date", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if d < data_min:
                continue
            # filter state/territory (passes se bater em qualquer um dos sets)
            sc_match = bool(state_filter) and (g.get("state_code") or "").upper() in state_filter
            tid_match = bool(territory_filter) and g.get("territory_id") in territory_filter
            if not sc_match and not tid_match:
                continue
            gazettes_unicos[key] = g
    log.info(f"gazettes unicos pos-filtro: {len(gazettes_unicos)}")

    if not gazettes_unicos:
        return 0

    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        for g in gazettes_unicos.values():
            _STATS["buscados"] += 1
            texto = fetch_texto(g.get("txt_url"), g.get("url"), sess=sess)
            if not texto:
                continue
            _STATS["fetched_text"] += 1
            if not KEYWORDS_OBRA_RE.search(texto):
                continue
            _STATS["filtrados_keyword"] += 1
            dados = haiku_extrair(client, g, texto)
            _STATS["extracoes_haiku"] += 1
            if not dados or not dados.get("eh_obra_real"):
                _STATS["haiku_skip"] += 1
                continue
            try:
                oid = inserir_obra(conn, g, dados)
                if oid:
                    _STATS["novos"] += 1
            except Exception as e:
                log.warning(f"insert erro {g.get('url')!r}: {e}")
                _STATS["erros"] += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        conn.close()

    log.info(f"DOE done — {_STATS}")
    return 0


if __name__ == "__main__":
    try:
        _rc = main() or 0
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001
        _STATS["erros"] = max(_STATS["erros"], 1)
        log.error(f"DOE UNCAUGHT {type(_exc).__name__}: {_exc}")
        log.error(f"trace:\n{_tb.format_exc()}")
        sys.exit(1)
    sys.exit(_rc)
