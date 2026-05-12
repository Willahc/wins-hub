import re
import time
import logging
from typing import Optional
import requests

log = logging.getLogger("sales_intel.brasilapi")

# Token bucket simples (25 req/min)
_BUCKET = {"tokens": 25.0, "last": time.monotonic(), "rate": 25 / 60.0, "max": 25.0}


def _acquire_token():
    now = time.monotonic()
    elapsed = now - _BUCKET["last"]
    _BUCKET["tokens"] = min(_BUCKET["max"], _BUCKET["tokens"] + elapsed * _BUCKET["rate"])
    _BUCKET["last"] = now
    if _BUCKET["tokens"] < 1:
        wait = (1 - _BUCKET["tokens"]) / _BUCKET["rate"]
        time.sleep(wait)
        _BUCKET["tokens"] = 0.0
        _BUCKET["last"] = time.monotonic()
    else:
        _BUCKET["tokens"] -= 1


def normalizar_cnpj(cnpj_raw: str) -> str:
    return re.sub(r"\D", "", cnpj_raw or "")


def validar_cnpj_dv(cnpj: str) -> bool:
    c = normalizar_cnpj(cnpj)
    if len(c) != 14 or c == c[0] * 14:
        return False
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    p2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    s = sum(int(c[i]) * p1[i] for i in range(12))
    r = s % 11
    if int(c[12]) != (0 if r < 2 else 11 - r):
        return False
    s = sum(int(c[i]) * p2[i] for i in range(13))
    r = s % 11
    return int(c[13]) == (0 if r < 2 else 11 - r)


def buscar_dados_cnpj(cnpj: str) -> Optional[dict]:
    """Consulta BrasilAPI. Retorna dict ou None (404/erros).
    Levanta ValueError se DV invalido (anti-alucinacao: nao bater API com CNPJ malformado)."""
    c = normalizar_cnpj(cnpj)
    if not validar_cnpj_dv(c):
        raise ValueError(f"CNPJ {cnpj} invalido (DV1+DV2)")
    url = f"https://brasilapi.com.br/api/cnpj/v1/{c}"
    waits = [1, 4]
    for attempt in range(3):
        _acquire_token()
        try:
            r = requests.get(url, timeout=10,
                             headers={"User-Agent": "WiNS Hub sales_intel"})
            if r.status_code == 404:
                log.info(f"BrasilAPI 404 cnpj={c}")
                return None
            if r.status_code == 200:
                return r.json()
            log.warning(f"BrasilAPI HTTP {r.status_code} cnpj={c} attempt={attempt+1}")
        except requests.RequestException as e:
            log.warning(f"BrasilAPI exception cnpj={c} attempt={attempt+1}: {e}")
        if attempt < len(waits):
            time.sleep(waits[attempt])
    return None


def mapear_para_dossier(dados: dict) -> Optional[dict]:
    if not dados:
        return None
    cap = dados.get("capital_social")
    cap_f: Optional[float] = None
    if cap is not None:
        try:
            cap_f = float(cap)
        except (TypeError, ValueError):
            cap_f = None
    return {
        "cnpj": str(dados.get("cnpj") or ""),
        "razao_social": dados.get("razao_social"),
        "nome_fantasia": dados.get("nome_fantasia") or None,
        "natureza_juridica": dados.get("natureza_juridica"),
        "cnae_fiscal": str(dados.get("cnae_fiscal") or "") or None,
        "cnae_descricao": dados.get("cnae_fiscal_descricao"),
        "situacao_cadastral": dados.get("descricao_situacao_cadastral"),
        "uf": dados.get("uf"),
        "municipio": dados.get("municipio"),
        "capital_social": cap_f,
        "matriz": str(dados.get("identificador_matriz_filial") or "1") == "1",
    }
