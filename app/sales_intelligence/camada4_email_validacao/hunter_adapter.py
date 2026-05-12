"""Hunter.io adapter: email-verifier (validar) + email-finder (encontrar por nome).

Endpoints:
  GET /v2/email-verifier?email=X&api_key=K
  GET /v2/email-finder?domain=X&first_name=Y&last_name=Z&api_key=K

Hunter free tier: 25 buscas/mes. Plano starter ~$49/mes.
HUNTER_API_KEY ja existe no .env desta sessao.
"""
import logging
import os
from typing import Optional, Tuple

import requests

log = logging.getLogger("sales_intel.hunter_adapter")

HUNTER_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"
HUNTER_FIND_URL = "https://api.hunter.io/v2/email-finder"

# Mapeamento Hunter status -> nosso EmailStatusLiteral
HUNTER_STATUS_MAP = {
    "valid": "verified_smtp",
    "invalid": "invalid",
    "accept_all": "catch_all",     # Hunter detectou catch-all
    "webmail": "verified_mx",      # gmail/outlook: tem MX mas sem verify SMTP confiavel
    "disposable": "invalid",       # email temporario
    "unknown": "greylisted",
}


def _api_key() -> str:
    return os.getenv("HUNTER_API_KEY", "").strip()


def hunter_disponivel() -> bool:
    return bool(_api_key())


def verificar_email(email: str) -> Optional[dict]:
    """Chama /v2/email-verifier. Retorna dict da Hunter ou None se erro."""
    key = _api_key()
    if not key:
        log.warning("HUNTER_API_KEY ausente, skip verifier")
        return None
    try:
        r = requests.get(HUNTER_VERIFY_URL,
                         params={"email": email, "api_key": key},
                         timeout=12,
                         headers={"User-Agent": "WiNS Hub sales_intel"})
        if r.status_code == 200:
            return r.json().get("data")
        if r.status_code == 429:
            log.warning("hunter rate limit (verifier)")
            return None
        log.warning(f"hunter verifier HTTP {r.status_code}")
        return None
    except (requests.RequestException, ValueError) as e:
        log.warning(f"hunter verifier exception {email}: {e}")
        return None


def encontrar_email(nome_completo: str, dominio: str) -> Optional[dict]:
    """Chama /v2/email-finder. Retorna dict da Hunter ou None se erro/sem hit."""
    key = _api_key()
    if not key:
        log.warning("HUNTER_API_KEY ausente, skip finder")
        return None
    if not (nome_completo and dominio):
        return None
    partes = nome_completo.strip().split()
    if len(partes) < 2:
        return None
    first = partes[0]
    last = partes[-1]
    try:
        r = requests.get(HUNTER_FIND_URL,
                         params={"domain": dominio, "first_name": first,
                                 "last_name": last, "api_key": key},
                         timeout=12,
                         headers={"User-Agent": "WiNS Hub sales_intel"})
        if r.status_code == 200:
            data = r.json().get("data") or {}
            if data.get("email"):
                return data
            return None
        if r.status_code == 429:
            log.warning("hunter rate limit (finder)")
            return None
        log.warning(f"hunter finder HTTP {r.status_code}")
        return None
    except (requests.RequestException, ValueError) as e:
        log.warning(f"hunter finder exception {nome_completo} @{dominio}: {e}")
        return None


def mapear_status_hunter(hunter_status: str) -> str:
    """Hunter 'result' -> nosso EmailStatusLiteral. Default greylisted."""
    return HUNTER_STATUS_MAP.get((hunter_status or "").lower(), "greylisted")
