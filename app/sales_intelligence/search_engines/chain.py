"""Fallback chain de search engines. Default: Brave -> Bing -> DDG.

Ordem inicial respeita: confiabilidade primeiro (Brave API), depois
fontes scraped sem key (Bing > DDG, dado que Bing tem CAPTCHA menos
agressivo que DDG). Cada engine na chain so eh tentado se anterior
retornar rate_limited ou nao-OK.
"""
import logging
import os
from typing import List, Optional

from sales_intelligence.search_engines.base import (
    SearchEngineAdapter, SearchResponse,
)
from sales_intelligence.search_engines.serper import SerperAdapter
from sales_intelligence.search_engines.brave import BraveAdapter
from sales_intelligence.search_engines.bing import BingAdapter
from sales_intelligence.search_engines.ddg import DDGAdapter

log = logging.getLogger("sales_intel.chain")


def _build_default_chain() -> List[SearchEngineAdapter]:
    """Constroi chain a partir do env WNS_SEARCH_ENGINES (csv) ou default.
    Default: serper > brave > bing > ddg (em ordem de confiabilidade)."""
    csv = (os.getenv("WNS_SEARCH_ENGINES") or "serper,brave,bing,ddg").lower().strip()
    nomes = [n.strip() for n in csv.split(",") if n.strip()]
    chain: List[SearchEngineAdapter] = []
    factories = {
        "serper": SerperAdapter,
        "brave": BraveAdapter,
        "bing": BingAdapter,
        "ddg": DDGAdapter,
    }
    for n in nomes:
        if n in factories:
            adapter = factories[n]()
            if adapter.available:
                chain.append(adapter)
            else:
                log.info(f"adapter '{n}' indisponivel, pulando")
    return chain


class SearchChain:
    """Tenta engines em ordem ate uma retornar resultado nao-rate-limited."""

    def __init__(self, adapters: Optional[List[SearchEngineAdapter]] = None):
        self.adapters = adapters if adapters is not None else _build_default_chain()
        if not self.adapters:
            log.warning("nenhum adapter disponivel na chain")

    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        last_resp = SearchResponse(engine="none", error="no_adapters")
        for adapter in self.adapters:
            resp = adapter.search(query, max_results=max_results)
            last_resp = resp
            # ok ou tem ao menos 1 resultado -> retornar
            if resp.ok and resp.results:
                log.info(f"chain hit em {adapter.name} ({len(resp.results)} results)")
                return resp
            # se rate-limited, tenta proximo
            if resp.rate_limited:
                log.info(f"chain {adapter.name} rate-limited, fallback")
                continue
            # se falhou mas nao rate-limit, tenta proximo (ex.: 5xx, network)
            if resp.error:
                log.info(f"chain {adapter.name} error={resp.error}, fallback")
                continue
            # status ok mas sem resultados — ainda vale tentar proximo?
            # decisao: NAO tentar (provavelmente query nao tem hits em geral)
            log.info(f"chain {adapter.name} status={resp.status} sem results, parando")
            return resp
        return last_resp


# instancia default global
default_chain = SearchChain()
