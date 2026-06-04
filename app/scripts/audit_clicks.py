"""Testa filtros clicáveis em /obras e /fornecedores via Playwright."""
import time
from playwright.sync_api import sync_playwright

BASE = "https://winshubcomercial.com.br"


def time_filter_click(page, sel_or_xpath, label):
    """Clica e mede tempo até próximo /api/obras ou /api/fornecedores response."""
    api_completed = []
    def on_resp(r):
        if ("/api/obras" in r.url or "/api/fornecedores" in r.url) and r.url.count("/") <= 5:
            api_completed.append((r.url, r.status, time.time()))
    page.on("response", on_resp)
    t0 = time.time()
    try:
        if sel_or_xpath.startswith("//") or sel_or_xpath.startswith("xpath="):
            page.locator(sel_or_xpath).first.click(timeout=8000)
        else:
            page.click(sel_or_xpath, timeout=8000)
    except Exception as e:
        print(f"  CLICK FAIL '{label}': {type(e).__name__}: {str(e)[:120]}")
        return None
    # Espera até API responder (8s max)
    deadline = time.time() + 8
    while time.time() < deadline:
        if api_completed:
            break
        page.wait_for_timeout(150)
    if api_completed:
        url, status, t = api_completed[-1]
        endpoint = url.replace(BASE,'').split('?')[0]
        params = url.split('?')[1][:80] if '?' in url else ''
        print(f"  '{label}' → {endpoint}?{params}: {(t-t0)*1000:.0f}ms (HTTP {status})")
        return t - t0
    print(f"  '{label}' → NO API CALL within 8s")
    return None


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width":1366,"height":900}, ignore_https_errors=True)

    # /OBRAS
    page = ctx.new_page()
    print("=== /obras filtros ===")
    print(f"Loading {BASE}/obras …")
    t0 = time.time()
    page.goto(f"{BASE}/obras", wait_until="networkidle", timeout=60_000)
    print(f"  load: {(time.time()-t0)*1000:.0f}ms")
    page.wait_for_timeout(1000)
    # Filtros visíveis (sidebar):
    # 1) ORDENAR — Maior CAPEX
    time_filter_click(page, "text=Maior CAPEX", "Ordenar: Maior CAPEX")
    page.wait_for_timeout(800)
    # 2) TIER — clicar em chip Ouro (sidebar normalmente tem checkbox/chip)
    time_filter_click(page, "text=Ouro", "Tier Ouro chip")
    page.wait_for_timeout(800)
    time_filter_click(page, "text=Prata", "Tier Prata chip")
    page.wait_for_timeout(800)
    # 3) SETOR
    time_filter_click(page, "text=Energia", "Setor Energia chip")
    page.wait_for_timeout(800)
    # 4) busca — input search
    try:
        search_input = page.locator("input[type='search'], input[placeholder*='busca'], input[placeholder*='Busc']").first
        search_input.fill("petrobras")
        time_filter_click(page, "input[type='search']", "Busca petrobras (focus)")
        # API call dispara on change debounced; aguardar
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  Search input: {type(e).__name__}: {e}")

    # /FORNECEDORES
    page2 = ctx.new_page()
    print("\n=== /fornecedores filtros ===")
    print(f"Loading {BASE}/fornecedores …")
    t0 = time.time()
    page2.goto(f"{BASE}/fornecedores", wait_until="networkidle", timeout=60_000)
    print(f"  load: {(time.time()-t0)*1000:.0f}ms")
    page2.wait_for_timeout(1000)
    # UF SP, porte GRANDE, busca
    time_filter_click(page2, "text=SP", "UF SP chip")
    page2.wait_for_timeout(800)
    time_filter_click(page2, "text=Grande", "Porte Grande chip")
    page2.wait_for_timeout(800)

    # Coletar console errors finais
    print("\n=== Final summary ===")
    browser.close()
