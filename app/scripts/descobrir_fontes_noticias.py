#!/usr/bin/env python3
"""B1 — Descoberta de fontes RSS via Playwright + análise de cobertura.

Pra cada um dos 15 portais:
  1. Playwright acessa homepage (headless + UA real)
  2. Detecta Cloudflare (status/HTML)
  3. Procura <link rel="alternate" type="application/rss+xml">
  4. Pra cada RSS: GET via feedparser (com UA real) + valida XML
  5. Calcula items_24h, items_7d, gap_médio_horas

Output: /tmp/fontes_descobertas.csv
"""
import csv
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import feedparser
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("descobrir_fontes")

PORTAIS = [
    ("valor", "https://valor.globo.com/"),
    ("infomoney", "https://www.infomoney.com.br/"),
    ("moneytimes", "https://www.moneytimes.com.br/"),
    ("g1_economia", "https://g1.globo.com/economia/"),
    ("neofeed", "https://neofeed.com.br/"),
    ("braziljournal", "https://braziljournal.com/"),
    ("exame", "https://exame.com/"),
    ("istoedinheiro", "https://istoedinheiro.com.br/"),
    ("agenciainfra", "https://agenciainfra.com/"),
    ("broadcast", "https://www.broadcast.com.br/"),
    ("clickpetroleoegas", "https://clickpetroleoegas.com.br/"),
    ("epbr", "https://epbr.com.br/"),
    ("monitormercantil", "https://monitormercantil.com.br/"),
    ("panoramafarmaceutico", "https://panoramafarmaceutico.com.br/"),
    ("portalmineracao", "https://portalmineracao.com.br/"),
]

UA_REAL = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

OUT_CSV = "/tmp/fontes_descobertas.csv"


def detectar_rss_links(html, base_url):
    """Extrai todos <link rel="alternate" type="application/rss+xml" href="...">"""
    # vários patterns possíveis
    rss = []
    for m in re.finditer(
        r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']',
        html, re.I):
        rss.append(m.group(1))
    for m in re.finditer(
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\']',
        html, re.I):
        rss.append(m.group(1))
    # tentar URLs comuns como fallback
    for path in ["/feed/", "/rss/", "/feed", "/rss", "/feed.xml", "/index.xml"]:
        rss.append(base_url.rstrip("/") + path)
    # dedup mantendo ordem
    seen = set()
    out = []
    for u in rss:
        if u.startswith("/"):
            u = base_url.rstrip("/") + u
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def analisar_rss(url):
    """Retorna dict com items_24h, items_7d, gap_medio_horas, ok."""
    try:
        feed = feedparser.parse(url, agent=UA_REAL, request_headers={"User-Agent": UA_REAL})
    except Exception as e:
        return {"ok": False, "erro": str(e)[:100]}

    status = feed.get("status")
    bozo = feed.bozo
    entries = feed.entries or []
    if not entries or status == 403 or status == 404:
        return {"ok": False, "status": status, "bozo": bozo, "n_entries": len(entries)}

    now = datetime.now(timezone.utc)
    pubs = []
    for e in entries:
        pp = e.get("published_parsed") or e.get("updated_parsed")
        if pp:
            try:
                pubs.append(datetime(*pp[:6], tzinfo=timezone.utc))
            except Exception:
                pass

    items_24h = sum(1 for p in pubs if (now - p) < timedelta(hours=24))
    items_7d = sum(1 for p in pubs if (now - p) < timedelta(days=7))

    pubs_sorted = sorted(pubs[:6], reverse=True)
    gaps = []
    for i in range(len(pubs_sorted) - 1):
        delta_h = (pubs_sorted[i] - pubs_sorted[i + 1]).total_seconds() / 3600.0
        if delta_h > 0:
            gaps.append(delta_h)
    gap_medio = sum(gaps) / len(gaps) if gaps else None

    return {
        "ok": True, "status": status, "bozo": bozo,
        "n_entries": len(entries),
        "items_24h": items_24h,
        "items_7d": items_7d,
        "gap_medio_horas": round(gap_medio, 1) if gap_medio else None,
    }


def estimar_cobertura(rss_url, entries_sample):
    """Pega títulos+summaries dos primeiros 10 items, conta hits em keywords industriais."""
    kws = ["investimento", "fábrica", "planta", "obra", "construção", "expansão",
           "anuncia", "bilh", "milh", "indústria", "fábrica", "data center",
           "mineração", "petróleo", "energia", "logística"]
    try:
        feed = feedparser.parse(rss_url, agent=UA_REAL, request_headers={"User-Agent": UA_REAL})
        amostra = []
        for e in (feed.entries or [])[:10]:
            t = (e.get("title") or "") + " " + (e.get("summary") or "")[:300]
            amostra.append(t.lower())
        if not amostra:
            return 0
        hits = sum(1 for t in amostra for k in kws if k in t)
        return hits  # raw count
    except Exception:
        return 0


def processar_portal(nome, url, browser):
    log.info(f"--- {nome} | {url} ---")
    result = {"portal": nome, "url": url, "tem_cf": "?", "rss_url": "",
              "rss_valido": False, "items_24h": 0, "items_7d": 0,
              "gap_medio_horas": "", "cobertura_industrial_estimada": 0,
              "obs": ""}

    page = browser.new_page()
    page.set_extra_http_headers({"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
    cf = False
    html = ""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(2)
        status = resp.status if resp else 0
        html = page.content()
        if status == 403 or "cf-mitigated" in str(resp.headers if resp else {}).lower() \
           or 'cf-challenge' in html.lower() or 'Just a moment' in html:
            cf = True
    except Exception as e:
        result["obs"] = f"playwright erro: {e!r}"[:120]
        page.close()
        return result
    page.close()
    result["tem_cf"] = "sim" if cf else "não"

    rss_links = detectar_rss_links(html, url)
    log.info(f"  rss candidatos: {len(rss_links)}")

    # testar cada RSS
    melhor = None
    for r_url in rss_links[:6]:
        info = analisar_rss(r_url)
        if info.get("ok") and info.get("n_entries", 0) >= 5:
            if not melhor or info["items_7d"] > melhor[1]["items_7d"]:
                melhor = (r_url, info)
    if melhor:
        result["rss_url"] = melhor[0]
        result["rss_valido"] = True
        result["items_24h"] = melhor[1]["items_24h"]
        result["items_7d"] = melhor[1]["items_7d"]
        result["gap_medio_horas"] = melhor[1]["gap_medio_horas"] or ""
        result["cobertura_industrial_estimada"] = estimar_cobertura(melhor[0], None)
    else:
        result["obs"] = (result["obs"] or "") + " | nenhum RSS válido com 5+ entries"
    return result


def main():
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA_REAL)
        for nome, url in PORTAIS:
            try:
                r = processar_portal(nome, url, ctx)
            except Exception as e:
                r = {"portal": nome, "url": url, "tem_cf": "?", "rss_url": "",
                     "rss_valido": False, "items_24h": 0, "items_7d": 0,
                     "gap_medio_horas": "", "cobertura_industrial_estimada": 0,
                     "obs": f"erro top: {e!r}"[:120]}
            rows.append(r)
        ctx.close()
        browser.close()

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    log.info(f"CSV salvo: {OUT_CSV}")

    # imprimir resumo
    rows_sorted = sorted(rows, key=lambda r: (r["rss_valido"], r["items_7d"]), reverse=True)
    print()
    print(f"{'portal':22s} {'cf':3s} {'rss?':4s} {'24h':>4s} {'7d':>4s} {'gap':>6s} {'cob':>4s}  obs")
    for r in rows_sorted:
        print(f"{r['portal']:22s} {r['tem_cf']:3s} {'OK' if r['rss_valido'] else 'no':4s} "
              f"{r['items_24h']:>4} {r['items_7d']:>4} {str(r['gap_medio_horas']):>6s} "
              f"{r['cobertura_industrial_estimada']:>4}  {r['obs'][:50]}")


if __name__ == "__main__":
    main()
