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
    nome = obra.get("empresa") or obra.get("razao")
    if not nome or not _KEY:
        return {}
    try:
        from populate_dominios import search_domain
        d = search_domain(nome, _KEY, _log)
        return {"dominio": d} if d else {}
    except Exception as e:
        _log.warning(f"serper falhou empresa={nome[:40]!r}: {e!r}")
        return {}
