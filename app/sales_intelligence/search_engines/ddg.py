"""DDG Search API adapter using keyless ddgs library (100% free & stable)."""
import logging

from sales_intelligence.search_engines.base import (
    SearchEngineAdapter, SearchResponse, SearchResult,
)

log = logging.getLogger("sales_intel.ddg")


class DDGAdapter(SearchEngineAdapter):
    name = "ddg"
    available = True

    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        log.info(f"ddgs GET query: {query[:120]}")
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                res_list = list(ddgs.text(query, max_results=max_results))
            
            results = []
            for r in res_list:
                results.append(SearchResult(
                    title=r.get("title", "")[:300],
                    url=r.get("href", "")[:500],
                    snippet=r.get("body", "")[:500],
                    raw_html="",
                    engine="ddg",
                ))
            log.info(f"ddgs status=200 results={len(results)}")
            return SearchResponse(
                results=results, raw_html="", engine="ddg",
                status=200, rate_limited=False,
            )
        except Exception as e:
            log.warning(f"ddgs exception: {e}")
            return SearchResponse(engine="ddg", error=str(e)[:200])
