import re
import logging
from typing import List
import requests

log = logging.getLogger("sales_intel.subdominios")

_CACHE: dict = {}


def listar_subdominios(dominio: str, limit: int = 50) -> List[str]:
    """Consulta crt.sh (Certificate Transparency). Retorna subdominios unicos do dominio raiz."""
    if not dominio:
        return []
    if dominio in _CACHE:
        return _CACHE[dominio]
    try:
        url = f"https://crt.sh/?q=%25.{dominio}&output=json"
        r = requests.get(url, timeout=15, headers={"User-Agent": "WiNS Hub sales_intel"})
        if r.status_code != 200:
            log.warning(f"crt.sh HTTP {r.status_code} dominio={dominio}")
            _CACHE[dominio] = []
            return []
        data = r.json() if r.text.strip() else []
    except (requests.RequestException, ValueError) as e:
        log.warning(f"crt.sh exception dominio={dominio}: {e}")
        _CACHE[dominio] = []
        return []

    subs = set()
    for entry in data:
        nv = entry.get("name_value", "") or ""
        for name in nv.split("\n"):
            name = name.strip().lower()
            if not name or "*" in name:  # ignora wildcards
                continue
            if not re.match(r"^[a-z0-9.-]+$", name):
                continue
            if name == dominio or name.endswith("." + dominio):
                subs.add(name)
    out = sorted(subs)[:limit]
    _CACHE[dominio] = out
    return out
