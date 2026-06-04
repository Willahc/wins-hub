"""Audit busca por texto em /obras (TikTok) e /fornecedores (Luxor / Nova Engevix)."""
import time, json
from playwright.sync_api import sync_playwright

BASE = "https://winshubcomercial.com.br"


def find_search_input(page):
    """Tenta diferentes seletores pra achar o campo de busca."""
    for sel in [
        "input[type='search']",
        "input[placeholder*='Buscar' i]",
        "input[placeholder*='Busca' i]",
        "input[placeholder*='Pesquis' i]",
        "input[placeholder*='fornecedor' i]",
        "input[placeholder*='razão' i]",
        "input[placeholder*='CNPJ' i]",
        "input[placeholder*='nome' i]",
        "input[placeholder*='empresa' i]",
        "input[x-model*='busca']",
        ".v2-search input",
        "input.v2-search__input",
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            try:
                if loc.is_visible():
                    return sel, loc
            except Exception:
                continue
    return None, None


def measure_search(page, label, term, debounce_ms=900):
    api_calls = []
    def on_resp(r):
        if ("/api/obras" in r.url or "/api/fornecedores" in r.url) and "?" in r.url:
            try:
                ms = (time.time())
            except Exception:
                ms = None
            api_calls.append({"url": r.url, "status": r.status, "ts": ms})
    page.on("response", on_resp)

    sel, loc = find_search_input(page)
    if not loc:
        print(f"  '{label}' — SEARCH INPUT NOT FOUND. Visible inputs:")
        inputs = page.evaluate("""() => {
            return [...document.querySelectorAll('input')].map(i => ({
                type: i.type, placeholder: i.placeholder, name: i.name,
                visible: i.offsetParent !== null, model: i.getAttribute('x-model')
            })).filter(i => i.visible);
        }""")
        for i in inputs[:8]: print(f"    {i}")
        return
    print(f"  '{label}' input found: {sel}")
    t0 = time.time()
    loc.fill("")
    loc.type(term, delay=80)  # digita char-a-char pra simular usuário real
    print(f"  typed '{term}' ({(time.time()-t0)*1000:.0f}ms)")
    # debounce + API
    t1 = time.time()
    page.wait_for_timeout(debounce_ms + 100)
    # esperar pela última API call relevante
    deadline = time.time() + 15
    last_call_at = 0
    while time.time() < deadline:
        if api_calls:
            last_call_at = api_calls[-1]["ts"]
            # Espera 500ms sem nova call
            page.wait_for_timeout(400)
            if api_calls[-1]["ts"] == last_call_at:
                break
        else:
            page.wait_for_timeout(200)
    if api_calls:
        last = api_calls[-1]
        endpoint = last["url"].replace(BASE,'').split('?')[0]
        params = last["url"].split('?',1)[1] if '?' in last["url"] else ''
        print(f"  '{label}' '{term}' → {endpoint}?{params[:120]}")
        print(f"    {len(api_calls)} API calls total | last: HTTP {last['status']} | digitação→resposta total: {(last_call_at - t0)*1000:.0f}ms")
    else:
        print(f"  '{label}' '{term}' — NO API CALLS after typing")

    # Quantos resultados aparecem no UI?
    try:
        page.wait_for_timeout(500)
        rows = page.evaluate("""() => {
            const cards = document.querySelectorAll('.v2-card, .card, .obra-card, .fornecedor-card, [data-obra-id], [data-cnpj]');
            return cards.length;
        }""")
        print(f"    UI cards visíveis: {rows}")
    except Exception:
        pass


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width":1366,"height":900}, ignore_https_errors=True)

    # ==== /obras busca tiktok ====
    page = ctx.new_page()
    print(f"\n=== /obras busca 'tiktok' ===")
    t0 = time.time()
    page.goto(f"{BASE}/obras", wait_until="networkidle", timeout=60_000)
    print(f"  initial load: {(time.time()-t0)*1000:.0f}ms")
    measure_search(page, "obras busca", "tiktok")
    page.wait_for_timeout(500)

    # ==== /fornecedores busca Luxor ====
    page2 = ctx.new_page()
    print(f"\n=== /fornecedores busca 'Luxor' ===")
    t0 = time.time()
    page2.goto(f"{BASE}/fornecedores", wait_until="networkidle", timeout=60_000)
    print(f"  initial load: {(time.time()-t0)*1000:.0f}ms")
    measure_search(page2, "fornec busca", "Luxor")
    page2.wait_for_timeout(500)
    # limpa e busca segundo termo
    print(f"\n=== /fornecedores busca 'Nova Engevix' ===")
    measure_search(page2, "fornec busca 2", "Nova Engevix")

    browser.close()
