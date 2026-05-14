#!/usr/bin/env python3
"""Captador ANP — Previsao de Atividades e Investimentos Exploratorios.

V9 (14/05/2026): parser CSV + INSERT obras agregadas via flag --commit.

ESCOPO DO DADO:
  O CSV "previsao-atividades-investimentos-pte.csv" tem colunas:
    ATIVIDADE (Unidade) | AMBIENTE | ETAPA | Ano Referencia |
    Ano Atividade | QUANTIDADE | INVESTIMENTO (milhoes US$) |
    INVESTIMENTO (milhoes R$)
  Nao ha empresa / CNPJ / bloco / UF (so MAR ou TERRA) por linha.
  Sao agregados macro do setor E&P brasileiro. Cada linha vira uma
  "obra agregada" com empresa='ANP - Previsao E&P' (placeholder),
  id_externo deterministico. Util para dashboards de capex setorial,
  NAO para prospeccao de decisor.

USO:
    python /app/scripts/captar_anp.py             # scaffold (sem INSERT) — default
    python /app/scripts/captar_anp.py --commit    # upsert das agregadas em obras

STATS_JSON na ultima linha (orchestrator).
"""
from __future__ import annotations

import argparse
import atexit
import csv
import io
import json as _json
import logging
import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import psycopg2
import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("captar_anp")

FONTE = "anp"
PORTAL_URL = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/previsao-de-exploracao"
SUBPASTA_ARQUIVOS = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/arquivos-previsao-de-atividades-e-investimentos"

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
    "ckan_ok": 0,
    "xlsx_baixados": 0,
    "csv_baixados": 0,
    "sheets_descobertos": 0,
    "linhas_totais": 0,
    "csv_linhas_parseadas": 0,
    "obras_upsertadas": 0,
}


@atexit.register
def _emit_stats():
    print(f"STATS_JSON: {_json.dumps(_STATS)}", flush=True)


def tentar_ckan() -> bool:
    try:
        r = requests.get(
            "https://dados.gov.br/api/3/action/package_search?q=anp&rows=1",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except Exception as e:
        log.warning(f"CKAN HTTP erro: {e}")
        return False
    log.info(f"CKAN status={r.status_code}")
    if r.status_code == 200:
        try:
            return bool(r.json().get("success"))
        except Exception:
            return False
    return False


def coletar_links_xlsx_via_playwright(limite: int = 5) -> List[Dict[str, str]]:
    from playwright.sync_api import sync_playwright

    urls_tentativas = [PORTAL_URL, SUBPASTA_ARQUIVOS]
    coletados: List[Dict[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
        )
        page = ctx.new_page()
        for url in urls_tentativas:
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning(f"playwright goto {url}: {e}")
                continue
            items = page.eval_on_selector_all(
                "a",
                "els => els.map(e => ({"
                "  h: e.href, "
                "  t: (e.innerText || '').trim().slice(0, 80)"
                "})).filter(o => /\\.(xlsx|csv|xls)$/i.test(o.h))",
            )
            for it in items:
                if not it.get("h"):
                    continue
                coletados.append({"titulo": (it.get("t") or "")[:200], "url": it["h"]})
            if coletados:
                break
        browser.close()
    # dedup por url
    seen = set()
    uniq: List[Dict[str, str]] = []
    for it in coletados:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq[:limite]


def baixar_xlsx_info(url: str) -> Dict[str, Any]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        log.warning("openpyxl nao disponivel")
        return {}
    try:
        r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        log.warning(f"download {url}: {e}")
        return {}
    try:
        wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    except Exception as e:
        log.warning(f"openpyxl parse {url}: {e}")
        return {}
    info: Dict[str, Any] = {"sheets": [], "total_rows": 0}
    for s in wb.sheetnames:
        ws = wb[s]
        rows = ws.max_row or 0
        cols = ws.max_column or 0
        info["sheets"].append({"name": s, "rows": rows, "cols": cols})
        info["total_rows"] += rows
    return info


# ── V9: parser CSV ANP previsao de atividades + investimentos ────────
def _to_float_br(s: str):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    # ANP usa "," como decimal e sem milhares. Tolera "1.234,56" tambem.
    s = s.replace(".", "").replace(",", ".") if "," in s else s
    try:
        return float(s)
    except ValueError:
        return None


def _slug(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:60]


def baixar_csv_anp(url: str) -> List[Dict[str, Any]]:
    """Baixa o CSV ANP de previsao e parseia linhas em dicts normalizados."""
    try:
        r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        log.warning(f"download CSV {url}: {e}")
        return []
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows: List[Dict[str, Any]] = []
    for row in reader:
        atividade = (row.get("ATIVIDADE (Unidade)") or "").strip()
        ambiente = (row.get("AMBIENTE") or "").strip()
        etapa = (row.get("ETAPA") or "").strip()
        ano_ref = (row.get("Ano Referência") or row.get("Ano Referencia") or "").strip()
        ano_atv = (row.get("Ano Atividade") or "").strip()
        quantidade = _to_float_br(row.get("QUANTIDADE"))
        invest_usd = _to_float_br(row.get("INVESTIMENTO (milhões US$)"))
        invest_brl = _to_float_br(row.get("INVESTIMENTO (milhões R$)"))
        if not atividade or not ano_atv:
            continue
        rows.append({
            "atividade": atividade,
            "ambiente": ambiente,
            "etapa": etapa,
            "ano_referencia": ano_ref,
            "ano_atividade": ano_atv,
            "quantidade": quantidade,
            "investimento_usd_milhoes": invest_usd,
            "investimento_brl_milhoes": invest_brl,
        })
    return rows


def upsert_obras_agregadas(rows: List[Dict[str, Any]], conn) -> int:
    """Upsert de obras agregadas ANP. id_externo deterministico evita duplicacao."""
    cur = conn.cursor()
    upserted = 0
    for r in rows:
        id_externo = "anp_pte_{ano_ref}_{ano_atv}_{ativ}_{amb}_{etapa}".format(
            ano_ref=r["ano_referencia"] or "x",
            ano_atv=r["ano_atividade"] or "x",
            ativ=_slug(r["atividade"]),
            amb=_slug(r["ambiente"]),
            etapa=_slug(r["etapa"]),
        )[:120]
        valor_reais = (r["investimento_brl_milhoes"] or 0) * 1_000_000 or None
        nome = f"ANP PTE {r['ano_atividade']} — {r['atividade']}"[:200]
        descricao = (
            f"ANP Previsão E&P — atividade={r['atividade']}; ambiente={r['ambiente']}; "
            f"etapa={r['etapa']}; ano_referencia={r['ano_referencia']}; "
            f"ano_atividade={r['ano_atividade']}; quantidade={r['quantidade']}; "
            f"investimento_US$_mi={r['investimento_usd_milhoes']}; "
            f"investimento_R$_mi={r['investimento_brl_milhoes']}"
        )
        cur.execute("""
            INSERT INTO obras (
                nome, empresa, descricao, valor_estimado,
                fonte, fonte_tipo, id_externo, status, data_anuncio
            ) VALUES (
                %s, 'ANP - Previsão E&P', %s, %s,
                'anp_pte', 'OFICIAL', %s, 'anunciado', CURRENT_DATE
            )
            ON CONFLICT (id_externo) DO UPDATE SET
                valor_estimado = EXCLUDED.valor_estimado,
                descricao = EXCLUDED.descricao,
                valor_atualizado_em = NOW()
        """, (nome, descricao, valor_reais, id_externo))
        upserted += cur.rowcount
    conn.commit()
    cur.close()
    return upserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persistir obras agregadas em obras (default OFF — preserva scaffold)",
    )
    args, _ = parser.parse_known_args()

    log.info(f"ANP v9 — modo={'COMMIT' if args.commit else 'scaffold'}")

    if tentar_ckan():
        _STATS["ckan_ok"] = 1
        log.info("CKAN respondeu 200 — possivelmente o bloqueio foi transitorio")
    else:
        log.info("CKAN bloqueado (esperado) — fallback Playwright")

    links = coletar_links_xlsx_via_playwright(limite=5)
    _STATS["buscados"] = len(links)
    log.info(f"Arquivos candidatos: {len(links)}")
    for li in links:
        log.info(f"  - {li['titulo']!r} -> {li['url'][:120]}")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        for li in links[:5]:
            url = li["url"]
            titulo = li["titulo"]
            is_csv = url.lower().endswith(".csv")
            is_pte_csv = is_csv and "previsao-atividades-investimentos" in url.lower()

            if is_pte_csv:
                rows = baixar_csv_anp(url)
                if not rows:
                    continue
                _STATS["csv_baixados"] += 1
                _STATS["csv_linhas_parseadas"] += len(rows)
                _STATS["linhas_totais"] += len(rows)
                log.info(f"  + CSV {titulo!r}: {len(rows)} linhas parseadas")
                if args.commit:
                    n = upsert_obras_agregadas(rows, conn)
                    _STATS["obras_upsertadas"] += n
                    _STATS["novos"] += n
                    log.info(f"    upserted {n} obras agregadas (fonte=anp_pte)")
            elif url.lower().endswith((".xlsx", ".xls")):
                info = baixar_xlsx_info(url)
                if not info:
                    continue
                _STATS["xlsx_baixados"] += 1
                _STATS["sheets_descobertos"] += len(info["sheets"])
                _STATS["linhas_totais"] += info["total_rows"]
                log.info(f"  + XLSX {titulo!r}: {len(info['sheets'])} sheets, "
                         f"{info['total_rows']} linhas totais (NAO inserido — schema variavel)")
                for sh in info["sheets"][:5]:
                    log.info(f"      sheet={sh['name']!r} rows={sh['rows']} cols={sh['cols']}")
            else:
                log.info(f"  - skip (extensao nao suportada): {url[-60:]}")
    finally:
        conn.close()

    if _STATS["xlsx_baixados"] == 0 and _STATS["csv_baixados"] == 0:
        log.warning("Nenhum arquivo baixado — layout pode ter mudado ou anti-bot")
    log.info(f"ANP done — {_STATS}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
