"""Audit busca v2 — typing real, debounce track, end-to-end."""
import time, json
from playwright.sync_api import sync_playwright
BASE = "https://winshubcomercial.com.br"


def fill_and_measure(page, sel, term, label):
    """Fill input + medir tempo do último char digitado até API completar."""
    api_done_at = []
    def on_resp(r):
        if ("/api/obras" in r.url or "/api/fornecedores" in r.url) and "?" in r.url:
            api_done_at.append({"ts": time.time(), "url": r.url, "status": r.status})
    page.on("response", on_resp)
    inp = page.locator(sel).first
    inp.click()
    inp.fill("")  # limpa
    api_done_at.clear()
    # Digita progressivamente
    typed_at = time.time()
    for ch in term:
        inp.press(ch, delay=50)
    typed_done = time.time()
    print(f"  '{label}' '{term}' typed in {(typed_done-typed_at)*1000:.0f}ms")
    # Espera API (max 8s)
    deadline = time.time() + 8
    while time.time() < deadline:
        if api_done_at and (time.time() - api_done_at[-1]["ts"] > 0.5):
            break
        page.wait_for_timeout(150)
    if api_done_at:
        last = api_done_at[-1]
        api_latency = last["ts"] - typed_done
        endpoint = last["url"].replace(BASE,'').split('?')[0]
        params = last["url"].split('?',1)[1] if '?' in last["url"] else ''
        print(f"    {len(api_done_at)} API calls | last endpoint: {endpoint}")
        print(f"    last params: {params[:150]}")
        print(f"    last call →done: {api_latency*1000:.0f}ms (HTTP {last['status']})")
    else:
        print(f"    NO API CALLS after typing")
    page.wait_for_timeout(300)
    cards = page.evaluate("""() => {
        return document.querySelectorAll('article, .v2-card, [data-obra-id], [data-cnpj]').length;
    }""")
    print(f"    UI cards rendered: {cards}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width":1366,"height":900}, ignore_https_errors=True)

    # ==== OBRAS busca TikTok ====
    page = ctx.new_page()
    print("=== /obras carga inicial ===")
    t0 = time.time()
    page.goto(f"{BASE}/obras", wait_until="networkidle", timeout=60_000)
    print(f"  load: {(time.time()-t0)*1000:.0f}ms")
    page.wait_for_timeout(800)
    # Achar input — sabido placeholder começa com "Buscar"
    fill_and_measure(page, "input[placeholder*='Buscar' i]", "TikTok", "obras TikTok")
    print()
    fill_and_measure(page, "input[placeholder*='Buscar' i]", "petrobras", "obras petrobras")

    # ==== FORNECEDORES busca Luxor ====
    page2 = ctx.new_page()
    print("\n=== /fornecedores carga inicial ===")
    t0 = time.time()
    page2.goto(f"{BASE}/fornecedores", wait_until="networkidle", timeout=60_000)
    print(f"  load: {(time.time()-t0)*1000:.0f}ms")
    page2.wait_for_timeout(800)
    fill_and_measure(page2, "input[placeholder*='fornecedor' i]", "Luxor", "fornec Luxor")
    print()
    fill_and_measure(page2, "input[placeholder*='fornecedor' i]", "Engevix", "fornec Engevix")

    browser.close()
