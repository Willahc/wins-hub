from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field


# Status alinhados com check constraint de empresa_decisores_cache.email_status:
# 'pending' | 'inferred_pattern' | 'verified_mx' | 'verified_smtp' | 'invalid' | 'bounce'
EmailStatusLiteral = Literal[
    "pending", "inferred_pattern", "verified_mx", "verified_smtp",
    "invalid", "bounce", "catch_all", "greylisted",
]


class EmailValidacao(BaseModel):
    email: str
    status: EmailStatusLiteral
    sintaxe_ok: bool
    mx_record: Optional[str] = None         # mx_host com menor preferencia
    smtp_response_code: Optional[int] = None
    smtp_response_msg: Optional[str] = None
    catch_all: bool = False                  # dominio aceita qualquer @endereco
    fonte_validacao: str                     # 'smtp' | 'hunter' | 'cache'
    confianca: Literal["alta", "media", "baixa"] = "baixa"
    validated_at: datetime = Field(default_factory=datetime.utcnow)


class EmailEncontrado(BaseModel):
    """Resultado de email-finder Hunter (busca por nome+dominio)."""
    nome: str
    dominio: str
    email: Optional[str] = None
    score_hunter: Optional[int] = None       # 0-100 da Hunter
    fonte: str = "hunter_finder"
    encontrado_em: datetime = Field(default_factory=datetime.utcnow)
