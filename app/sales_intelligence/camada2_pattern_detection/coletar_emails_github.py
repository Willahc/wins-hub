import logging
import os
from typing import List
import requests

log = logging.getLogger("sales_intel.coletar_github")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # opcional, sem PAT funciona com rate limit baixo


def coletar_emails_github(dominio: str, limit: int = 20) -> List[str]:
    if not dominio:
        return []
    headers = {"User-Agent": "WiNS Hub sales_intel",
               "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    url = "https://api.github.com/search/users"
    params = {"q": f"{dominio} in:email", "per_page": min(limit, 100)}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
        if r.status_code == 403:
            log.warning(f"github rate limit (403) dominio={dominio}")
            return []
        if r.status_code != 200:
            log.warning(f"github HTTP {r.status_code} dominio={dominio}")
            return []
        items = r.json().get("items", [])
    except (requests.RequestException, ValueError) as e:
        log.warning(f"github exception dominio={dominio}: {e}")
        return []

    # API /search/users nao retorna email direto; fazer lookup individual eh caro.
    # v1: extrair login + tentar montar email so se tiver site_admin info.
    # Aproveitar so emails que aparecem em campo email se publico.
    emails: set = set()
    for it in items[:limit]:
        em = it.get("email")
        if em and "@" in em and em.lower().endswith("@" + dominio):
            emails.add(em.lower())
    return sorted(emails)
