#!/usr/bin/env python3
"""
Reconhecimento de fontes de CNPJ pra concessionarias ANTT rodoviarias.
Reaproveita Chromium do sistema (/usr/bin/chromium-browser) via Playwright.

NAO faz UPDATE no banco. Apenas exploracao + screenshots + report.
"""
import re
import sys
from playwright.sync_api import sync_playwright

CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\b\d{14}\b")
CHROMIUM = "/usr/bin/chromium-browser"
LAUNCH_ARGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]

def cnpj_valido(c):
    c = re.sub(r'\D','', c or '')
    if len(c) != 14 or c == c[0]*14: return False
    p1=[5,4,3,2,9,8,7,6,5,4,3,2]; p2=[6,5,4,3,2,9,8,7,6,5,4,3,2]
    s = sum(int(c[i])*p1[i] for i in range(12)); r = s % 11
    if int(c[12]) != (0 if r<2 else 11-r): return False
    s = sum(int(c[i])*p2[i] for i in range(13)); r = s % 11
    return int(c[13]) == (0 if r<2 else 11-r)

def explore_url(page, url, screenshot_path=None, wait_ms=3000):
    print(f"\n--- {url}")
    try:
        resp = page.goto(url, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        status = resp.status if resp else "?"
        body = page.content()
        cnpjs_found = CNPJ_RE.findall(body)
        cnpjs_unicos = sorted(set(cnpjs_found))
        valid = [c for c in cnpjs_unicos if cnpj_valido(c)]
        print(f"  HTTP {status} | bytes={len(body)} | CNPJs match={len(cnpjs_unicos)} | validos={len(valid)}")
        if screenshot_path:
            page.screenshot(path=screenshot_path, full_page=False)
            print(f"  screenshot -> {screenshot_path}")
        if valid:
            print(f"  amostra validos: {valid[:5]}")
        elif cnpjs_unicos:
            print(f"  amostra (DV invalido): {cnpjs_unicos[:5]}")
        return {"url": url, "status": status, "size": len(body),
                "cnpjs_total": len(cnpjs_unicos), "cnpjs_validos": len(valid),
                "amostra_validos": valid[:5]}
    except Exception as e:
        print(f"  FALHOU: {str(e)[:200]}")
        return {"url": url, "erro": str(e)[:200]}

def main():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=LAUNCH_ARGS)
        ctx = browser.new_context(user_agent="Mozilla/5.0 WiNS HUB recon")
        page = ctx.new_page()

        print("="*72)
        print("A3-a) portal.antt.gov.br")
        print("="*72)
        results["a"] = explore_url(page,
            "https://portal.antt.gov.br/concessionarias-rodoviarias",
            "/tmp/antt_concessionarias.png")

        print("\n" + "="*72)
        print("A3-b) gov.br/antt")
        print("="*72)
        results["b"] = explore_url(page,
            "https://www.gov.br/antt/pt-br/assuntos/rodovias/concessoes-rodoviarias",
            "/tmp/govbr_antt.png")

        print("\n" + "="*72)
        print("A3-c) DuckDuckGo HTML site:antt.gov.br CNPJ concessionarias")
        print("="*72)
        results["c"] = explore_url(page,
            "https://html.duckduckgo.com/html/?q=site%3Aantt.gov.br+concessionarias+CNPJ",
            "/tmp/ddg_antt.png", wait_ms=2000)

        print("\n" + "="*72)
        print("A3-d) dados.antt.gov.br dataset concessionarias")
        print("="*72)
        results["d"] = explore_url(page,
            "https://dados.antt.gov.br/dataset/concessionarias-rodoviarias",
            "/tmp/dados_antt.png")

        browser.close()

    print("\n" + "="*72)
    print("RESUMO")
    print("="*72)
    for k, v in results.items():
        print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
