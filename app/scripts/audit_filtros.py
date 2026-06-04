"""Audita /obras e /fornecedores via Playwright — perf, network, filtros, console."""
import time
from playwright.sync_api import sync_playwright

BASE = "https://winshubcomercial.com.br"


def measure(page, url, label):
    """Carrega url, mede load time, retorna métricas."""
    console_logs = []
    page.on("console", lambda m: console_logs.append(f"[{m.type}] {m.text[:200]}"))
    page.on("pageerror", lambda e: console_logs.append(f"[ERR] {e}"))
    requests = []
    page.on("response", lambda r: requests.append({
        "url": r.url, "status": r.status,
        "ms": None,  # filled below from timing
    }))
    t0 = time.time()
    resp = page.goto(url, wait_until="networkidle", timeout=60_000)
    t1 = time.time()
    # Performance API
    perf = page.evaluate("""() => {
        const t = performance.timing;
        const nav = performance.getEntriesByType('navigation')[0] || {};
        return {
            domContentLoaded: t.domContentLoadedEventEnd - t.navigationStart,
            loadEvent: t.loadEventEnd - t.navigationStart,
            ttfb: t.responseStart - t.navigationStart,
            domInteractive: t.domInteractive - t.navigationStart,
            transferSize: nav.transferSize || 0,
            domNodes: document.querySelectorAll('*').length,
        };
    }""")
    print(f"\n== {label} → {url} ==")
    print(f"  total time:        {(t1-t0)*1000:.0f}ms")
    print(f"  TTFB:              {perf['ttfb']}ms")
    print(f"  domInteractive:    {perf['domInteractive']}ms")
    print(f"  domContentLoaded:  {perf['domContentLoaded']}ms")
    print(f"  loadEvent:         {perf['loadEvent']}ms")
    print(f"  transferSize:      {perf['transferSize']/1024:.1f} KB")
    print(f"  domNodes:          {perf['domNodes']}")
    print(f"  total requests:    {len(requests)}")
    if console_logs:
        print(f"  console msgs ({len(console_logs)}):")
        for m in console_logs[:8]: print(f"    {m}")
    # Slowest API calls (api/* > 500ms inferred via load order? — usaremos resourceTiming)
    slow = page.evaluate("""() => {
        return performance.getEntriesByType('resource')
          .filter(r => r.name.includes('/api/') || r.name.includes('/static/'))
          .map(r => ({name: r.name, duration: Math.round(r.duration), size: r.transferSize}))
          .sort((a,b) => b.duration - a.duration)
          .slice(0, 15);
    }""")
    print(f"  TOP 15 slowest /api/ or /static/:")
    for r in slow:
        nm = r['name'].replace(BASE,'').split('?')[0][:60]
        params = r['name'].split('?')[1][:40] if '?' in r['name'] else ''
        print(f"    {r['duration']:6}ms | {r['size']:8} | {nm} {params}")
    return slow


def click_filter_measure(page, label, selector, follow_up_wait_xhr=True):
    """Clica num filtro e mede tempo até /api/obras retornar."""
    t0 = time.time()
    api_started = [False]
    api_finished_at = [None]
    def on_resp(r):
        if "/api/obras" in r.url and not r.url.endswith("/"):
            api_finished_at[0] = time.time()
    page.on("response", on_resp)
    try:
        page.click(selector, timeout=5_000)
    except Exception as e:
        print(f"  CLICK FAILED {selector}: {e}")
        return
    if follow_up_wait_xhr:
        # Wait até /api/obras responder
        for _ in range(60):
            if api_finished_at[0]:
                break
            page.wait_for_timeout(200)
    t1 = api_finished_at[0] or time.time()
    print(f"  filter '{label}' → API: {(t1-t0)*1000:.0f}ms")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="WinsHub-Audit/1.0",
        ignore_https_errors=True,
    )
    page = ctx.new_page()

    # ============== /obras ==============
    measure(page, f"{BASE}/obras", "OBRAS — cold load")
    # snapshot html structure dos filtros
    filtros_html = page.evaluate("""() => {
        // tenta achar containers de filtros
        const candidates = ['aside', '.filters', '#filtros', '[x-data*="filter"]', '.sidebar'];
        for (const sel of candidates) {
            const el = document.querySelector(sel);
            if (el) return {sel, html: el.outerHTML.substring(0, 2000)};
        }
        // last resort: estrutura geral
        return {sel: 'body', html: document.body.innerHTML.substring(0, 2000)};
    }""")
    print(f"\n  Filtros HTML matched on {filtros_html['sel']}:")
    print(f"  {filtros_html['html'][:1500]}")

    # ============== /fornecedores ==============
    page2 = ctx.new_page()
    measure(page2, f"{BASE}/fornecedores", "FORNECEDORES — cold load")
    filtros_html2 = page2.evaluate("""() => {
        const candidates = ['aside', '.filters', '#filtros', '[x-data*="filter"]', '.sidebar'];
        for (const sel of candidates) {
            const el = document.querySelector(sel);
            if (el) return {sel, html: el.outerHTML.substring(0, 2000)};
        }
        return {sel: 'body', html: document.body.innerHTML.substring(0, 2000)};
    }""")
    print(f"\n  Filtros HTML matched on {filtros_html2['sel']}:")
    print(f"  {filtros_html2['html'][:1500]}")

    browser.close()
