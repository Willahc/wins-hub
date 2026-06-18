#!/usr/bin/env python3
"""web_search_serper.py — adaptador free-first p/ o portão: empresa -> {dominio}
via Serper (reusa populate_dominios.search_domain, chain Serper>Brave>Bing>DDG).
Injetado em portao.avaliar(..., web_search_fn=web_search_fn). Decisor via web_search
fica p/ etapa posterior; domínio é o ganho principal (infere e-mail). Hunter NUNCA aqui."""
import os
import logging
import sys

sys.path.insert(0, "/app/scripts")
sys.path.insert(0, "/app")  # p/ a SearchChain (sales_intelligence.camada1) — resolver tier-1
_log = logging.getLogger("portao.serper")
_KEY = os.getenv("SERPER_API_KEY")


def web_search_fn(obra):
    """SÓ Serper HTTP-puro (_search_serper_direct). NUNCA usa search_domain/
    descobrir_dominio_via_chain — aquele caminho lança Playwright/Chromium, proibido na
    VPS 1vCPU (feedback_vps_overload_no_browser). Timeout 15s embutido no requests.post."""
    nome = obra.get("empresa") or obra.get("razao")
    if not nome or not _KEY:
        return {}
    try:
        from populate_dominios import _search_serper_direct
        d = _search_serper_direct(nome, _KEY, _log)
        return {"dominio": d} if d else {}
    except Exception as e:
        _log.warning(f"serper falhou empresa={nome[:40]!r}: {e!r}")
        return {}
