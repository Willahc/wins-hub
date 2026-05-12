"""Validacao MX record do dominio. Cache local na sessao (TTL 5 min)."""
import logging
import time
from typing import List, Optional, Tuple

import dns.resolver
import dns.exception

log = logging.getLogger("sales_intel.validar_mx")

_CACHE: dict = {}  # dominio -> (timestamp, [mx_hosts])
_CACHE_TTL = 300  # 5 min


def buscar_mx(dominio: str) -> List[str]:
    """Retorna lista de mx_hosts ordenados por preferencia. [] se nenhum."""
    if not dominio:
        return []
    now = time.time()
    if dominio in _CACHE:
        ts, hosts = _CACHE[dominio]
        if now - ts < _CACHE_TTL:
            return hosts
    try:
        answers = dns.resolver.resolve(dominio, "MX", lifetime=5.0)
        mxs = sorted(((rdata.preference, str(rdata.exchange).rstrip("."))
                      for rdata in answers), key=lambda x: x[0])
        hosts = [h for _, h in mxs]
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.exception.Timeout) as e:
        log.debug(f"MX lookup falhou {dominio}: {e}")
        hosts = []
    except Exception as e:
        log.warning(f"MX lookup exception {dominio}: {e}")
        hosts = []
    _CACHE[dominio] = (now, hosts)
    return hosts


def tem_mx(dominio: str) -> Tuple[bool, Optional[str]]:
    """Retorna (tem_mx, primeiro_host_ou_None)."""
    hosts = buscar_mx(dominio)
    return (len(hosts) > 0, hosts[0] if hosts else None)
