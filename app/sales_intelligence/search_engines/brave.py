"""Brave Search API adapter. Ativa se BRAVE_API_KEY estiver no env.

Setup: cadastrar em https://api.search.brave.com/, obter key gratis (2k req/mes),
adicionar BRAVE_API_KEY=<key> ao /root/wins_hub/.env e restart api.
"""
import logging
import os
from typing import List

import requests

from sales_intelligence.search_engines.base import (
    SearchEngineAdapter, SearchResponse, SearchResult,
)

log = logging.getLogger("sales_intel.brave")

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveAdapter(SearchEngineAdapter):
    name = "brave"

    def __init__(self):
        self.key = os.getenv("BRAVE_API_KEY", "").strip()
        self.available = bool(self.key)
        if not self.available:
            log.info("brave adapter desabilitado (BRAVE_API_KEY ausente)")

    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        if not self.available:
            return SearchResponse(engine="brave", error="no_api_key")
        try:
            r = requests.get(
                BRAVE_ENDPOINT,
                params={"q": query, "count": min(max_results, 20)},
                headers={
                    "X-Subscription-Token": self.key,
                    "Accept": "application/json",
                },
                timeout=15,
            )
            status = r.status_code
            if status == 429:
                return SearchResponse(engine="brave", status=429, rate_limited=True)
            if status != 200:
                return SearchResponse(engine="brave", status=status,
                                       error=f"http_{status}")
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning(f"brave exception: {e}")
            return SearchResponse(engine="brave", error=str(e)[:200])

        results: List[SearchResult] = []
        web = (data.get("web") or {}).get("results", [])
        for item in web[:max_results]:
            results.append(SearchResult(
                title=(item.get("title") or "")[:300],
                url=(item.get("url") or "")[:500],
                snippet=(item.get("description") or "")[:500],
                raw_html="",
                engine="brave",
            ))
        log.info(f"brave status=200 results={len(results)}")
        return SearchResponse(results=results, raw_html="",
                               engine="brave", status=200)
