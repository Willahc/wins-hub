import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.search_engines.base import SearchResult, SearchResponse, SearchEngineAdapter
from sales_intelligence.search_engines.chain import SearchChain


class _MockAdapter(SearchEngineAdapter):
    def __init__(self, name, response):
        self.name = name
        self.available = True
        self._response = response

    def search(self, query, max_results=20):
        return self._response


def test_chain_primeiro_engine_ok():
    primeiro = _MockAdapter("brave", SearchResponse(
        results=[SearchResult(title="X", url="https://linkedin.com/in/x",
                              snippet="Joao Silva - Manager at Petrobras", engine="brave")],
        raw_html="<html>ok</html>", engine="brave", status=200,
    ))
    segundo = _MockAdapter("bing", SearchResponse(engine="bing"))
    chain = SearchChain(adapters=[primeiro, segundo])
    r = chain.search("test query")
    assert r.engine == "brave"
    assert len(r.results) == 1


def test_chain_fallback_em_rate_limit():
    primeiro = _MockAdapter("brave", SearchResponse(
        engine="brave", status=429, rate_limited=True))
    segundo = _MockAdapter("bing", SearchResponse(
        results=[SearchResult(title="X", url="https://linkedin.com/in/x",
                              snippet="Maria Silva", engine="bing")],
        raw_html="<html>ok</html>", engine="bing", status=200))
    chain = SearchChain(adapters=[primeiro, segundo])
    r = chain.search("test query")
    assert r.engine == "bing"
    assert len(r.results) == 1


def test_chain_fallback_em_erro():
    primeiro = _MockAdapter("brave", SearchResponse(engine="brave", error="network"))
    segundo = _MockAdapter("bing", SearchResponse(
        results=[SearchResult(title="X", url="https://linkedin.com/in/y",
                              snippet="Carlos", engine="bing")],
        raw_html="<html>ok</html>", engine="bing", status=200))
    chain = SearchChain(adapters=[primeiro, segundo])
    r = chain.search("query")
    assert r.engine == "bing"


def test_chain_para_em_engine_ok_sem_results():
    """Se engine retorna OK mas sem hits, NAO tenta proximo (otimizacao)."""
    primeiro = _MockAdapter("bing", SearchResponse(
        results=[], raw_html="<html>vazio</html>", engine="bing", status=200))
    segundo = _MockAdapter("ddg", SearchResponse(engine="ddg"))
    chain = SearchChain(adapters=[primeiro, segundo])
    r = chain.search("query")
    assert r.engine == "bing"
    assert r.results == []


def test_chain_todos_falham():
    primeiro = _MockAdapter("brave", SearchResponse(engine="brave", status=429, rate_limited=True))
    segundo = _MockAdapter("bing", SearchResponse(engine="bing", status=429, rate_limited=True))
    chain = SearchChain(adapters=[primeiro, segundo])
    r = chain.search("query")
    assert r.results == []


def test_chain_vazia():
    chain = SearchChain(adapters=[])
    r = chain.search("query")
    assert r.error == "no_adapters"


def test_brave_indisponivel_sem_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    from sales_intelligence.search_engines.brave import BraveAdapter
    a = BraveAdapter()
    assert a.available is False
    r = a.search("query")
    assert r.error == "no_api_key"


def test_search_response_ok_property():
    r1 = SearchResponse(status=200, raw_html="<html/>")
    assert r1.ok is True
    r2 = SearchResponse(status=200, raw_html="", rate_limited=False)
    assert r2.ok is False  # raw_html vazio
    r3 = SearchResponse(status=429, raw_html="<html/>", rate_limited=True)
    assert r3.ok is False
