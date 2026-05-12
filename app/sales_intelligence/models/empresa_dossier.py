from datetime import datetime, date
from typing import Optional, List, Literal
from pydantic import BaseModel, Field


class DominioOficial(BaseModel):
    dominio: str
    confianca: Literal["alta", "media", "baixa"]
    fonte: str
    score_fuzzy: Optional[float] = None
    head_status: Optional[int] = None


class WhoisInfo(BaseModel):
    email_administrativo: Optional[str] = None
    telefone: Optional[str] = None
    data_registro: Optional[date] = None
    registrante: Optional[str] = None
    fonte_metodo: Literal["rdap", "whois_texto", "falhou"] = "falhou"


class EmpresaDossier(BaseModel):
    cnpj: str
    razao_social: Optional[str] = None
    nome_fantasia: Optional[str] = None
    natureza_juridica: Optional[str] = None
    cnae_fiscal: Optional[str] = None
    cnae_descricao: Optional[str] = None
    situacao_cadastral: Optional[str] = None
    uf: Optional[str] = None
    municipio: Optional[str] = None
    capital_social: Optional[float] = None
    matriz: bool = True

    tipo_organizacao: Optional[str] = None
    spv_ou_matriz: Optional[Literal["matriz_grupo", "spv_operadora", "filial", "holding"]] = None
    grupo_controlador: Optional[str] = None
    confianca_classificacao: float = 0.0

    dominio_oficial: Optional[DominioOficial] = None
    subdominios: List[str] = Field(default_factory=list)
    whois: Optional[WhoisInfo] = None

    confianca_geral: float = 0.0
    fontes_utilizadas: List[str] = Field(default_factory=list)
    coletado_em: datetime = Field(default_factory=datetime.utcnow)
