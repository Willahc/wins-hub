"""DDG HTML endpoint scraper (sem API key)."""
import logging
import re
from html import unescape
from urllib.parse import quote_plus, unquote

# playwright importado lazy dentro de DDGAdapter.search (pode nao estar instalado).
# Padrao espelhado de bing.py e coletar_emails_site.py.
try:
    import playwright.sync_api  # noqa: F401 (probe)
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from sales_intelligence.search_engines.base import (
    SearchEngineAdapter, SearchResponse, SearchResult,
)

log = logging.getLogger("sales_intel.ddg")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"


def _extract_results(html: str) -> list:
    if not html:
        return []
    out = []
    seen = set()
    # DDG HTML: <div class="result"> com result__title result__url result__snippet
    for m in re.finditer(r'<div[^>]+class="[^"]*\bresult\b[^"]*"[^>]*>(.*?)(?=<div[^>]+class="[^"]*\bresult\b|</body)',
                          html, re.DOTALL):
        bloco = m.group(1)
        url_m = re.search(r'(?:uddg=)?(https?%3[Aa]%2F%2F[^"&]+|https?://[^"]+)', bloco)
        if not url_m:
            continue
        url = unquote(unescape(url_m.group(1)))
        if url in seen:
            continue
        seen.add(url)
        title_m = re.search(r'class="[^"]*result__title[^"]*">(.*?)</', bloco, re.DOTALL)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
        snip_m = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</', bloco, re.DOTALL)
        snippet = re.sub(r"<[^>]+>", " ", snip_m.group(1)).strip() if snip_m else ""
        snippet = re.sub(r"\s+", " ", unescape(snippet))[:500]
        out.append(SearchResult(
            title=re.sub(r"\s+", " ", unescape(title))[:300],
            url=url[:500],
            snippet=snippet,
            raw_html=bloco[:2000],
            engine="ddg",
        ))
    return out


class DDGAdapter(SearchEngineAdapter):
    name = "ddg"
    available = True

    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        if not PLAYWRIGHT_AVAILABLE:
            log.warning("ddg skip: playwright indisponivel (lib nao instalada)")
            return SearchResponse(engine="ddg", error="playwright_unavailable")

        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        log.info(f"ddg GET {url[:120]}")
        try:
            from playwright.sync_api import sync_playwright  # lazy
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path="/usr/bin/chromium-browser", headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                page = browser.new_context(user_agent=UA).new_page()
                r = page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                status = r.status if r else 0
                html = page.content() if status in (200, 202) else ""
                browser.close()
        except Exception as e:
            log.warning(f"ddg exception: {e}")
            return SearchResponse(engine="ddg", error=str(e)[:200])

        # DDG sob rate-limit retorna 202 com HTML pequeno
        rate_limited = status in (412, 429, 403) or (status == 202 and len(html) < 5000)
        results = _extract_results(html) if html else []
        log.info(f"ddg status={status} bytes={len(html)} results={len(results)}")
        return SearchResponse(
            results=results, raw_html=html, engine="ddg",
            status=status, rate_limited=rate_limited,
        )
