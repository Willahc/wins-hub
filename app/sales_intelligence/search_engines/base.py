"""Interface comum para search engines (Brave, Bing, DDG)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SearchResult:
    """Resultado normalizado de search engine."""
    title: str
    url: str
    snippet: str
    raw_html: str = ""        # bloco HTML em torno do resultado (parsing fino)
    engine: str = ""          # 'brave' | 'bing' | 'ddg'


@dataclass
class SearchResponse:
    """Resposta de uma chamada de search."""
    results: List[SearchResult] = field(default_factory=list)
    raw_html: str = ""        # HTML completo da pagina de resultados
    engine: str = ""
    status: int = 0           # HTTP status; 0 = nao chegou a response
    rate_limited: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.rate_limited and bool(self.raw_html)


class SearchEngineAdapter(ABC):
    """Adapter base. Cada engine implementa search() retornando SearchResponse."""

    name: str = "base"
    available: bool = True   # False se faltar config (ex.: API key)

    @abstractmethod
    def search(self, query: str, max_results: int = 20) -> SearchResponse:
        ...
