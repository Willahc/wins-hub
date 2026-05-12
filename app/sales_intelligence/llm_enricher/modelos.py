"""Modelos Pydantic para llm_enricher."""
from typing import Optional, Literal
from pydantic import BaseModel, Field


class DecisorInput(BaseModel):
    nome_pessoa: str
    cargo_raw: str
    snippet_origem: str
    url_origem: Optional[str] = None


class FiltroResult(BaseModel):
    trabalha_atualmente: bool
    confianca: Literal["alta", "media", "baixa"]
    empresa_atual_inferida: Optional[str] = None
    empresa_anterior_inferida: Optional[str] = None
    razao: str
    custo_usd: float = Field(default=0.0)
    tokens_input: int = 0
    tokens_output: int = 0
