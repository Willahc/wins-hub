"""Bing scraper via Playwright (sem API key)."""
import logging
import re
from html import unescape
from typing import List
from urllib.parse import quote_plus

# playwright importado lazy dentro de BingAdapter.search (pode nao estar instalado).
# Padrao espelhado de coletar_emails_site.py (Camada 2).
try:
    import playwright.sync_api  # noqa: F401 (probe)
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from sales_intelligence.search_engines.base import (
    SearchEngineAdapter, SearchResponse, SearchResult,
)

log = logging.getLogger("sales_intel.bing")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"


def _extract_results(html: str) -> List[SearchResult]:
    """Extrai resultados do Bing. Padrao SERP: <li class='b_algo'> com <h2><a>...</a></h2>."""
    if not html:
        return []
    out: List[SearchResult] = []
    seen_urls = set()
    # cada bloco b_algo
    for m in re.finditer(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', html, re.DOTALL):
        bloco = m.group(1)
        # url: <a href="...">
        url_m = re.search(r'<a[^>]+href="(https?://[^"]+)"', bloco)
        if not url_m:
            continue
        url = unescape(url_m.group(1))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # title: <h2>...<a>TITLE</a>...</h2>
        title_m = re.search(r'<h2[^>]*>(.*?)</h2>', bloco, re.DOTALL)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
        # snippet
        snip_m = re.search(r'<p[^>]*>(.*?)</p>', bloco, re.DOTALL)
        snippet = re.sub(r"<[^>]+>", " ", snip_m.group(1)).strip() if snip_m else ""
        snippet = re.sub(r"\s+", " ", unescape(snippet))[:500]
        out.append(SearchResult(
            title=re.sub(r"\s+", " ", unescape(title))[:300],
            url=url[:500],
            snippet=snippet,
            raw_html=bloco[:2000],
            engine="bing",
        ))
    return out


class BingAdapter(SearchEngineAdapter):
    name = "bing"
    available = True

    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        if not PLAYWRIGHT_AVAILABLE:
            log.warning("bing skip: playwright indisponivel (lib nao instalada)")
            return SearchResponse(engine="bing", error="playwright_unavailable")

        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={min(max_results, 50)}"
        log.info(f"bing GET {url[:120]}")
        try:
            from playwright.sync_api import sync_playwright  # lazy
            with sync_playwright() as p:
                import os
                proxy_url = os.getenv("TOR_PROXY_URL")
                proxy_config = {"server": proxy_url} if proxy_url else None
                browser = p.chromium.launch(
                    headless=True,
                    proxy=proxy_config,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                page = browser.new_context(user_agent=UA).new_page()
                r = page.goto(url, timeout=25000, wait_until="domcontentloaded")
                status = r.status if r else 0
                if status == 200:
                    # esperar renderizar resultados (Bing usa JS pra hidratar)
                    try:
                        page.wait_for_selector("li.b_algo, ol#b_results", timeout=8000)
                    except Exception:
                        pass  # se nao aparecer, fica com o HTML que tem
                    page.wait_for_timeout(800)
                html = page.content() if status == 200 else ""
                browser.close()
        except Exception as e:
            log.warning(f"bing exception: {e}")
            return SearchResponse(engine="bing", error=str(e)[:200])

        # bing pode retornar 200 com CAPTCHA challenge; detectar por marcadores
        html_lower = html.lower()
        bot_check = (
            status in (429, 503)
            or "verify you are not a robot" in html_lower
            or "captcha" in html_lower[:50000]
            or '"b_results"' not in html  # SERP normal sempre tem o container b_results
        )
        rate_limited = bot_check
        results = _extract_results(html) if html else []
        log.info(f"bing status={status} bytes={len(html)} results={len(results)}")
        return SearchResponse(
            results=results, raw_html=html, engine="bing",
            status=status, rate_limited=rate_limited,
        )
