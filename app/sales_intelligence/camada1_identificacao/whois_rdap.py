import re
import logging
import subprocess
from datetime import date
from typing import Optional
import requests

log = logging.getLogger("sales_intel.whois_rdap")


def _parse_rdap(data: dict) -> dict:
    out = {"email_administrativo": None, "telefone": None, "data_registro": None,
           "registrante": None, "fonte_metodo": "rdap"}
    # eventos
    for ev in data.get("events", []):
        if ev.get("eventAction") in ("registration", "registered"):
            d = (ev.get("eventDate") or "")[:10]
            if d:
                try:
                    out["data_registro"] = date.fromisoformat(d)
                except ValueError:
                    pass
    # entidades (registrante, admin)
    for ent in data.get("entities", []):
        roles = ent.get("roles", []) or []
        vcard = ent.get("vcardArray", [])
        if not vcard or len(vcard) < 2:
            continue
        for prop in vcard[1]:
            if not isinstance(prop, list) or len(prop) < 4:
                continue
            kind, _, _, value = prop[0], prop[1], prop[2], prop[3]
            if kind == "fn" and not out["registrante"] and "registrant" in roles:
                out["registrante"] = str(value)
            elif kind == "email" and not out["email_administrativo"]:
                if "administrative" in roles or "registrant" in roles or "abuse" in roles:
                    out["email_administrativo"] = str(value).lower()
            elif kind == "tel" and not out["telefone"]:
                out["telefone"] = str(value)
    return out


def _parse_whois_texto(txt: str) -> dict:
    out = {"email_administrativo": None, "telefone": None, "data_registro": None,
           "registrante": None, "fonte_metodo": "whois_texto"}
    # email administrativo (varios formatos)
    m = re.search(r"(?im)^(?:e-?mail|admin-c|owner-c|admin email|registrant email)\s*:\s*([^\s]+@[^\s]+)", txt)
    if m:
        out["email_administrativo"] = m.group(1).lower()
    else:
        m = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", txt)
        if m:
            out["email_administrativo"] = m.group(0).lower()
    # telefone
    m = re.search(r"(?im)^(?:phone|telefone|registrant phone)\s*:\s*(.+)$", txt)
    if m:
        out["telefone"] = m.group(1).strip()
    # data registro
    m = re.search(r"(?im)^(?:created|creation date|registered|registered on)\s*:\s*(\d{4}-\d{2}-\d{2})", txt)
    if m:
        try:
            out["data_registro"] = date.fromisoformat(m.group(1))
        except ValueError:
            pass
    # registrante
    m = re.search(r"(?im)^(?:owner|registrant|titular|registrant name)\s*:\s*(.+)$", txt)
    if m:
        out["registrante"] = m.group(1).strip()
    return out


def buscar_whois(dominio: str) -> dict:
    """Tenta RDAP (registro.br) primeiro; fallback whois textual via subprocess."""
    if not dominio:
        return {"fonte_metodo": "falhou", "email_administrativo": None,
                "telefone": None, "data_registro": None, "registrante": None}

    # 1) RDAP (apenas .br)
    if dominio.endswith(".br"):
        try:
            r = requests.get(f"https://rdap.registro.br/domain/{dominio}", timeout=10,
                             headers={"User-Agent": "WiNS Hub sales_intel"})
            if r.status_code == 200:
                return _parse_rdap(r.json())
        except (requests.RequestException, ValueError) as e:
            log.warning(f"RDAP exception {dominio}: {e}")

    # 2) WHOIS texto
    try:
        proc = subprocess.run(["whois", dominio], capture_output=True, text=True, timeout=10)
        if proc.returncode in (0, 1):
            return _parse_whois_texto(proc.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning(f"whois exception {dominio}: {e}")

    return {"fonte_metodo": "falhou", "email_administrativo": None,
            "telefone": None, "data_registro": None, "registrante": None}
