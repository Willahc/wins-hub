"""Detecta dominios catch-all (aceitam qualquer @endereco -> validacao SMTP nao-confiavel)."""
import logging
import secrets
import time
from typing import Optional

from sales_intelligence.camada4_email_validacao.validar_mx import tem_mx
from sales_intelligence.camada4_email_validacao.validar_smtp import smtp_probe

log = logging.getLogger("sales_intel.catch_all")

_CACHE: dict = {}  # dominio -> (timestamp, is_catch_all)
_CACHE_TTL = 86400  # 24h


def is_catch_all(dominio: str) -> bool:
    """True se dominio aceita um email random inexistente. Cache 24h."""
    if not dominio:
        return False
    now = time.time()
    if dominio in _CACHE:
        ts, val = _CACHE[dominio]
        if now - ts < _CACHE_TTL:
            return val

    ok, mx = tem_mx(dominio)
    if not ok:
        _CACHE[dominio] = (now, False)
        return False

    # endereco random extremamente improvavel de existir
    random_local = "qx-" + secrets.token_hex(8)
    test_email = f"{random_local}@{dominio}"
    status, _, _ = smtp_probe(test_email, mx)
    catch_all = (status == "verified_smtp")
    _CACHE[dominio] = (now, catch_all)
    log.info(f"catch_all check {dominio} -> {catch_all}")
    return catch_all
