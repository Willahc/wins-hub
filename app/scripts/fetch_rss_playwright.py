#!/usr/bin/env python3
"""B3.1 — Fetch RSS via Playwright headless pra bypass Cloudflare challenge.

Uso programático:
    from fetch_rss_playwright import fetch_with_playwright
    xml = fetch_with_playwright("https://clickpetroleoegas.com.br/feed/", timeout=15.0)

Retorna conteúdo XML/text bruto (string) ou None se falha.

Estratégia:
1. Abre Chromium headless com UA real (não navegador-default)
2. Navega pra URL com networkidle (espera CF resolver)
3. Pega innerText do documento (CF retorna XML pré-parsed em <pre>) ou page.content() raw
4. Aceita CF challenge JS sleep de até `timeout` segundos
"""
import asyncio
import logging
import sys
from typing import Optional

log = logging.getLogger("fetch_rss_playwright")

UA_REAL = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


async def _fetch_async(url: str, timeout: float) -> Optional[str]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA_REAL,
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
        )
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            # CF challenge usa JS — aguardar networkidle ou um pouco
            try:
                await page.wait_for_load_state("networkidle", timeout=int(timeout * 1000))
            except Exception:
                pass

            html = await page.content()

            # Se ainda há challenge CF, espera mais alguns segundos
            if "Just a moment" in html or "cf-challenge" in html.lower() or "challenge-platform" in html.lower():
                log.info(f"CF challenge detectado em {url}; aguardando {timeout}s...")
                await page.wait_for_timeout(int(timeout * 1000))
                html = await page.content()

            # Pra RSS feeds, o conteúdo XML real está em <pre> (Chromium renderiza assim)
            # ou direto no body se servidor retornou texto cru.
            pre = await page.query_selector("pre")
            if pre:
                return await pre.inner_text()
            return html
        except Exception as e:
            log.error(f"Playwright erro em {url}: {e}")
            return None
        finally:
            await ctx.close()
            await browser.close()


def fetch_with_playwright(url: str, timeout: float = 15.0) -> Optional[str]:
    """Sync wrapper. Retorna conteúdo do RSS via Playwright ou None se falhar."""
    try:
        return asyncio.run(_fetch_async(url, timeout))
    except Exception as e:
        log.error(f"fetch_with_playwright fatal em {url}: {e}")
        return None


if __name__ == "__main__":
    # CLI test
    if len(sys.argv) < 2:
        print("Uso: python fetch_rss_playwright.py <url> [timeout]")
        sys.exit(1)
    url = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    content = fetch_with_playwright(url, timeout)
    if content:
        print(f"\n=== {len(content)} chars ===")
        print(content[:2000])
    else:
        print("FALHA")
        sys.exit(2)
