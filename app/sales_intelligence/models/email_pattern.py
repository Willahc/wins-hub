from datetime import datetime
from typing import List, Literal
from pydantic import BaseModel, Field


class EmailPattern(BaseModel):
    padrao: str  # ex: "{first}.{last}", "{f}{last}", "{first}"
    confianca: Literal["alta", "media", "baixa"]
    exemplos: List[str]  # emails reais que validaram o padrao
    amostra_total: int  # quantos emails foram analisados (todos tipos)
    pessoas_count: int = 0  # quantos foram classificados como 'pessoa' (anti-alucinacao)
    dominio_origem: str
    detectado_em: datetime = Field(default_factory=datetime.utcnow)
