"""Modelos Pydantic para outreach."""
from typing import Optional, Literal, List
from pydantic import BaseModel, Field


class OutreachInput(BaseModel):
    # Decisor
    nome_pessoa: str
    cargo_raw: str
    empresa_nome: str
    email: str
    snippet_origem: str = ""

    # Obra de gancho
    obra_nome: str
    obra_valor_formatado: str  # "R$ 384 bi" (vem pronto do DB)
    obra_fase: str             # human-readable apos conversao no contextos.py
    obra_descricao: Optional[str] = None
    obra_uf: Optional[str] = None

    # Fornecedor cliente (piloto: exemplo demo)
    fornecedor_nome: str
    fornecedor_servicos: List[str]
    fornecedor_referencias: Optional[List[str]] = None

    # Metadata
    estilo: Literal["consultivo", "direto", "tecnico"] = "consultivo"
    idioma: Literal["pt-br", "en"] = "pt-br"


class OutreachOutput(BaseModel):
    assunto: str
    corpo: str
    cta: str
    raciocinio: str
    custo_usd: float = Field(default=0.0)
    tokens_input: int = 0
    tokens_output: int = 0
