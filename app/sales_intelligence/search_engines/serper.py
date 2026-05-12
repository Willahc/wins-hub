"""Serper.dev adapter (Google Search via API).

Setup: cadastrar em https://serper.dev/ (2500 buscas free no signup).
Adicionar SERPER_API_KEY=<key> ao /root/wins_hub/.env.

Endpoint: POST https://google.serper.dev/search
Header: X-API-KEY: <key>
Body: {"q": "<query>", "num": 10}
"""
import logging
import os
from typing import List

import requests

from sales_intelligence.search_engines.base import (
    SearchEngineAdapter, SearchResponse, SearchResult,
)

log = logging.getLogger("sales_intel.serper")

SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperAdapter(SearchEngineAdapter):
    name = "serper"

    def __init__(self):
        self.key = os.getenv("SERPER_API_KEY", "").strip()
        self.available = bool(self.key)
        if not self.available:
            log.info("serper adapter desabilitado (SERPER_API_KEY ausente)")

    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        if not self.available:
            return SearchResponse(engine="serper", error="no_api_key")
        try:
            r = requests.post(
                SERPER_ENDPOINT,
                json={"q": query, "num": min(max_results, 100)},
                headers={"X-API-KEY": self.key, "Content-Type": "application/json"},
                timeout=15,
            )
            status = r.status_code
            if status == 429:
                return SearchResponse(engine="serper", status=429, rate_limited=True)
            if status == 401:
                return SearchResponse(engine="serper", status=401,
                                       error="invalid_api_key")
            if status != 200:
                return SearchResponse(engine="serper", status=status,
                                       error=f"http_{status}")
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning(f"serper exception: {e}")
            return SearchResponse(engine="serper", error=str(e)[:200])

        results: List[SearchResult] = []
        for item in (data.get("organic") or [])[:max_results]:
            results.append(SearchResult(
                title=(item.get("title") or "")[:300],
                url=(item.get("link") or "")[:500],
                snippet=(item.get("snippet") or "")[:500],
                raw_html="",
                engine="serper",
            ))
        log.info(f"serper status=200 results={len(results)}")
        return SearchResponse(results=results, raw_html="",
                               engine="serper", status=200)
