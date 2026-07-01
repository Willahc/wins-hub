#!/usr/bin/env python3
from playwright.sync_api import sync_playwright
import time, os
SHOTS = "/tmp/shots"; os.makedirs(SHOTS, exist_ok=True)
BASE = "http://localhost:8000"
paginas = [
    ("01_landing", "/login", False),
    ("02_ranking", "/score", False),
    ("03_app",     "/",      True),
]
with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width":1280,"height":900}, device_scale_factor=2)
    for nome, rota, full in paginas:
        try:
            pg.goto(BASE+rota, wait_until="networkidle", timeout=40000)
            time.sleep(4)  # Alpine renderizar + dados da API
            pg.screenshot(path=f"{SHOTS}/{nome}.png", full_page=False)
            # tamanho do arquivo p/ saber se renderizou
            sz = os.path.getsize(f"{SHOTS}/{nome}.png")
            print(f"{nome}: ok ({sz//1024} KB)")
        except Exception as e:
            print(f"{nome}: ERRO {str(e)[:70]}")
    b.close()
