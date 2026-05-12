#!/usr/bin/env python3
"""
Validador CSP via Chromium headless (Playwright).

Navega pelas paginas principais, captura console messages e erros de rede,
filtra por keywords CSP, agrupa por pagina. Aguarda 3s pos-load para o beacon
CSP report ser enviado.

Exit 0 se nada legitimo aparecer; 1 se houver violacoes.

Uso: docker exec ... ou direto no host (precisa pip install playwright +
Chromium do sistema em /usr/bin/chromium-browser).
"""
import re
import sys
import time
from playwright.sync_api import sync_playwright

PAGES = [
    "https://winshubcomercial.com.br/",
    "https://winshubcomercial.com.br/login",
    "https://winshubcomercial.com.br/score",
    "https://winshubcomercial.com.br/como-funciona",
    "https://winshubcomercial.com.br/vendas",
]

CSP_KEYWORDS = (
    "content security policy",
    "refused to",
    "[report only]",
    "violated directive",
    "violates the following content security policy",
)

CHROMIUM = "/usr/bin/chromium-browser"
WAIT_AFTER_LOAD_MS = 3000
NAV_TIMEOUT_MS = 20000


def is_csp_message(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in CSP_KEYWORDS)


def main() -> int:
    findings = {url: {"csp": [], "errors": [], "net4xx5xx": [], "load_error": None} for url in PAGES}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(ignore_https_errors=False)

        for url in PAGES:
            page = context.new_page()
            console_msgs = []
            page_errors = []
            net_errors = []

            page.on("console", lambda msg: console_msgs.append((msg.type, msg.text)))
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            def on_response(resp):
                try:
                    if resp.status >= 400:
                        net_errors.append((resp.status, resp.url))
                except Exception:
                    pass
            page.on("response", on_response)

            try:
                page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="networkidle")
                page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
            except Exception as e:
                findings[url]["load_error"] = str(e)
                page.close()
                continue

            for level, text in console_msgs:
                if is_csp_message(text):
                    findings[url]["csp"].append(f"[{level}] {text}")
                elif level in ("error", "warning"):
                    findings[url]["errors"].append(f"[{level}] {text[:300]}")
            for exc in page_errors:
                findings[url]["errors"].append(f"[pageerror] {exc[:300]}")
            for status, ru in net_errors:
                findings[url]["net4xx5xx"].append(f"[{status}] {ru}")

            page.close()

        browser.close()

    print("=" * 72)
    print("RELATORIO DE VALIDACAO CSP - HEADLESS CHROMIUM")
    print("=" * 72)

    total_csp = 0
    for url, data in findings.items():
        print(f"\n--- {url}")
        if data["load_error"]:
            print(f"  LOAD ERROR: {data['load_error'][:200]}")
            continue
        if data["csp"]:
            print(f"  Violacoes CSP ({len(data['csp'])}):")
            for v in data["csp"]:
                print(f"    {v[:300]}")
            total_csp += len(data["csp"])
        else:
            print("  Violacoes CSP: nenhuma")
        if data["net4xx5xx"]:
            print(f"  Erros de rede 4xx/5xx ({len(data['net4xx5xx'])}):")
            for e in data["net4xx5xx"][:10]:
                print(f"    {e[:200]}")
        if data["errors"]:
            print(f"  Outros erros/warnings ({len(data['errors'])}):")
            for e in data["errors"][:5]:
                print(f"    {e[:200]}")

    print("\n" + "=" * 72)
    print(f"TOTAL violacoes CSP capturadas no console: {total_csp}")
    print("=" * 72)

    return 1 if total_csp > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
