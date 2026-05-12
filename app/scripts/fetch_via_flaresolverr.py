#!/usr/bin/env python3
"""B2 — Helper para fetch HTML/XML via FlareSolverr (bypass Cloudflare).

Uso programático:
    from fetch_via_flaresolverr import fetch_via_flaresolverr
    body = fetch_via_flaresolverr("https://clickpetroleoegas.com.br/feed/")

FlareSolverr roda em http://flaresolverr:8191 (rede docker interna) ou
127.0.0.1:8191 (host). Detecta env FLARESOLVERR_URL.

Retorna conteúdo HTML/XML como string ou None se falhar.
"""
import logging
import os
import sys
import time
from typing import Optional

import requests

log = logging.getLogger("fetch_via_flaresolverr")

DEFAULT_URL = "http://flaresolverr:8191/v1"
TIMEOUT_FS = 75  # client-side timeout (s); maxTimeout interno = 60s


def fetch_via_flaresolverr(url: str, max_timeout_ms: int = 60000,
                            retries: int = 1, fs_endpoint: Optional[str] = None) -> Optional[str]:
    """Resolve CF challenge via FlareSolverr e retorna o body HTML/XML resolvido."""
    fs_url = fs_endpoint or os.getenv("FLARESOLVERR_URL", DEFAULT_URL)
    payload = {"cmd": "request.get", "url": url, "maxTimeout": max_timeout_ms}
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(fs_url, json=payload, timeout=TIMEOUT_FS)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                log.warning(f"FS HTTP {r.status_code} tentativa {attempt+1}: {r.text[:150]}")
                continue
            data = r.json()
            if data.get("status") != "ok":
                last_err = f"FS status={data.get('status')} msg={data.get('message','?')}"
                log.warning(f"FS status erro tentativa {attempt+1}: {last_err}")
                continue
            sol = data.get("solution") or {}
            body = sol.get("response")
            if not body:
                last_err = "no body in solution"
                continue
            cf_status = sol.get("status")
            log.info(f"FS resolveu {url} (CF status={cf_status}, body={len(body)} chars)")
            return body
        except requests.RequestException as e:
            last_err = str(e)
            log.warning(f"FS request erro tentativa {attempt+1}: {e}")
            if attempt < retries:
                time.sleep(2)
            continue
    log.error(f"FS exaustou retries para {url}: {last_err}")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Uso: python fetch_via_flaresolverr.py <url>")
        sys.exit(1)
    body = fetch_via_flaresolverr(sys.argv[1])
    if body:
        print(f"\n=== {len(body)} chars ===")
        print(body[:2000])
    else:
        sys.exit(2)
