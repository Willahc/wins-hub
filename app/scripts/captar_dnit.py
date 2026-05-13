#!/usr/bin/env python3
"""Captador DNIT (Departamento Nacional de Infraestrutura de Transportes).

NOTA SOBRE ESCOPO (sprint dia 5 / Sessao 2):
  Brief original: captar via servicos.dnit.gov.br/dadosabertos. Esses subdominios
  retornam HTTP 000 (DNS/bloqueio geo) do nosso container.

  Fallback: gov.br/dnit/pt-br (portal federal) responde 200 mas e HTML
  navegacional, nao tem API/CSV/XLSX estruturados publicos para download
  facil de obras nominais.

  Esta versao funciona como SCAFFOLD: tenta gov.br/dnit/licitacoes, filtra
  por keyword, e marca obras candidatas. Volume real depende de redesign
  per-licitacao.

STATS_JSON na ultima linha. Idempotente via id_externo='DNIT:<sha1>'.
"""
from __future__ import annotations

import atexit
import hashlib
import io
import json as _json
import logging
import os
import re
import sys
import traceback as _tb
from datetime import date
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import psycopg2
import requests
from brutils import is_valid_cnpj


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_dnit")

FONTE = "dnit"
URL_BASE = "https://www.gov.br/dnit/pt-br/assuntos/licitacoes"
URL_BASE_FALLBACK = "https://www.gov.br/dnit/pt-br"

KEYWORDS_OBRA = re.compile(
    r"(licita[çc][ãa]o|obra|construc(?:ao|oes)|pavimenta|"
    r"r\$\s*\d[\d\.\,\s]*\s*(?:milh|bilh)|"
    r"rodovia|ponte|viaduto|trecho|edital)",
    re.IGNORECASE,
)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

_STATS = {"buscados": 0, "novos": 0, "erros": 0, "filtrados_keyword": 0}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def coletar_links_licitacoes() -> List[Dict[str, str]]:
    """Tenta gov.br/dnit/licitacoes; fallback portal raiz."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36"})
    out: List[Dict[str, str]] = []
    for url in (URL_BASE, URL_BASE_FALLBACK):
        try:
            r = sess.get(url, timeout=30)
            if r.status_code != 200:
                continue
        except Exception as e:
            log.warning(f"fetch {url}: {e}")
            continue
        hrefs = re.findall(r'<a\s+[^>]*href="([^"]+)"[^>]*>([^<]{10,200})</a>',
                           r.text, flags=re.IGNORECASE)
        from urllib.parse import urljoin
        for u, t in hrefs:
            t = t.strip()
            if not KEYWORDS_OBRA.search(t):
                continue
            full = urljoin(url, u)
            out.append({"url": full, "titulo": t[:200]})
        if out:
            break
    # dedup
    seen = set()
    uniq = []
    for o in out:
        if o["url"] in seen:
            continue
        seen.add(o["url"])
        uniq.append(o)
    return uniq[:30]


def id_ext(url: str) -> str:
    return "DNIT:" + hashlib.sha1(url.encode()).hexdigest()[:16]


def inserir_placeholder(conn, link: Dict[str, str]) -> Optional[str]:
    """Insere obra placeholder com info do link (sem Haiku — econômico).
    Setor INFRAESTRUTURA default. Mais detalhamento fica pra enriquecimento futuro."""
    nome = (link.get("titulo") or "DNIT licitacao")[:200]
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO obras (
                nome, empresa, cnpj, uf, municipio, setor,
                valor_estimado, fase, fonte, fonte_tipo,
                url_fonte, status, data_anuncio, confianca_extracao,
                descricao, descricao_sintetica, id_externo
            ) VALUES (
                %s, NULL, NULL, NULL, NULL, 'INFRAESTRUTURA',
                NULL, 'LICITACAO_ABERTA', %s, 'OFICIAL',
                %s, 'anunciado', %s, 0.5,
                %s, false, %s
            )
            ON CONFLICT (id_externo) DO NOTHING
            RETURNING id
        """, (
            nome, FONTE, link.get("url", ""),
            date.today(),
            (link.get("titulo") or "")[:1000],
            id_ext(link.get("url") or ""),
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else None


def main() -> int:
    log.info("DNIT scaffold — coletando links de licitacoes via gov.br/dnit")
    links = coletar_links_licitacoes()
    _STATS["buscados"] = len(links)
    _STATS["filtrados_keyword"] = len(links)  # ja filtrados por keyword
    log.info(f"links com keyword obra/licitacao: {len(links)}")

    if not links:
        log.warning("Nenhum link encontrado — portal gov.br/dnit pode ter mudado layout")
        return 0

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        for li in links[:30]:
            try:
                oid = inserir_placeholder(conn, li)
                if oid:
                    _STATS["novos"] += 1
            except Exception as e:
                log.warning(f"insert: {e}")
                _STATS["erros"] += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        conn.close()

    log.info(f"DNIT done — {_STATS}")
    return 0


if __name__ == "__main__":
    try:
        _rc = main() or 0
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001
        _STATS["erros"] = max(_STATS["erros"], 1)
        log.error(f"DNIT UNCAUGHT {type(_exc).__name__}: {_exc}")
        log.error(f"trace:\n{_tb.format_exc()}")
        sys.exit(1)
    sys.exit(_rc)
