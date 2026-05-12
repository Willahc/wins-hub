from datetime import datetime, date
from typing import Optional, Literal
from pydantic import BaseModel, Field


class DecisorBruto(BaseModel):
    """Resultado de uma fonte individual antes de consolidacao."""
    nome_pessoa: str
    cargo_raw: str
    cargo_normalizado: Optional[str] = None
    tipo_cargo: Optional[str] = None
    cargo_idioma: Optional[Literal["pt-br", "en"]] = None
    cargo_nivel: Optional[Literal["estrategico", "tatico", "operacional"]] = None
    linkedin_slug: Optional[str] = None
    snippet_origem: str = ""
    url_origem: str = ""
    confianca: Literal["alta", "media", "baixa"] = "baixa"
    fonte_descoberta: str  # 'ddg' | 'bing' | 'google' | 'crea' | 'cvm' | 'dou'
    marcadores_temporais: Optional[str] = None  # padrao temporal_gate (media conf)


class Decisor(BaseModel):
    """Decisor consolidado pronto pra cache."""
    cnpj: str
    nome_pessoa: str
    cargo_raw: str
    cargo_normalizado: Optional[str] = None
    tipo_cargo: str  # canonico, nunca None aqui (default OUTRO)
    cargo_idioma: Optional[Literal["pt-br", "en"]] = None
    cargo_nivel: Optional[Literal["estrategico", "tatico", "operacional"]] = None
    confianca: Literal["alta", "media", "baixa"]
    fonte_descoberta: str
    fonte_secundaria: Optional[str] = None
    snippet_origem: str = ""
    url_origem: str = ""
    linkedin_slug: Optional[str] = None
    email: Optional[str] = None
    email_status: Optional[str] = None
    score_relevancia: float = Field(ge=0.0, le=1.0, default=0.0)
    descoberto_em: datetime = Field(default_factory=datetime.utcnow)
    revalidacao: Optional[date] = None
    marcadores_temporais: Optional[str] = None  # propaga de DecisorBruto
