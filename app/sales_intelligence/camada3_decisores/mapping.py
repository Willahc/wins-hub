"""Camada 3 — taxonomia de cargos.

Estrategia: busca AMPLA (48 termos PT+EN), storage CANONICO (11 valores
Anderson). Cargo nao reconhecido vai pra OUTRO mas preserva cargo_raw.
"""
from typing import Optional
from unidecode import unidecode

CARGO_MAPPING_PTBR_EN = {
    # GERENTE_SUPRIMENTOS (8)
    "gerente de suprimentos": "GERENTE_SUPRIMENTOS",
    "procurement manager": "GERENTE_SUPRIMENTOS",
    "procurement director": "GERENTE_SUPRIMENTOS",
    "supply manager": "GERENTE_SUPRIMENTOS",
    "sourcing manager": "GERENTE_SUPRIMENTOS",
    "senior sourcing analyst": "GERENTE_SUPRIMENTOS",
    "strategic sourcing": "GERENTE_SUPRIMENTOS",
    "chief procurement officer": "GERENTE_SUPRIMENTOS",

    # GERENTE_COMPRAS (7)
    "gerente de compras": "GERENTE_COMPRAS",
    "purchasing manager": "GERENTE_COMPRAS",
    "buyer manager": "GERENTE_COMPRAS",
    "comprador senior": "GERENTE_COMPRAS",
    "senior buyer": "GERENTE_COMPRAS",
    "comprador tecnico": "GERENTE_COMPRAS",
    "technical buyer": "GERENTE_COMPRAS",

    # SUPPLY_CHAIN (5)
    "supply chain manager": "SUPPLY_CHAIN",
    "diretor de supply chain": "SUPPLY_CHAIN",
    "logistics director": "SUPPLY_CHAIN",
    "diretor de logistica": "SUPPLY_CHAIN",
    "head of supply chain": "SUPPLY_CHAIN",
    "capex": "SUPPLY_CHAIN",
    "gestao de capex": "SUPPLY_CHAIN",
    "capital expenditure": "SUPPLY_CHAIN",
    "investimentos e projetos": "SUPPLY_CHAIN",
    "gerente de investimentos": "SUPPLY_CHAIN",
    "coordenador de capex": "SUPPLY_CHAIN",

    # GERENTE_PROJETOS (9)
    "gerente de projetos": "GERENTE_PROJETOS",
    "project manager": "GERENTE_PROJETOS",
    "senior project manager": "GERENTE_PROJETOS",
    "diretor de projetos": "GERENTE_PROJETOS",
    "project director": "GERENTE_PROJETOS",
    "pmo": "GERENTE_PROJETOS",
    "head of pmo": "GERENTE_PROJETOS",
    "project coordinator": "GERENTE_PROJETOS",
    "coordenador de projetos": "GERENTE_PROJETOS",

    # GERENTE_ENGENHARIA (9)
    "gerente de engenharia": "GERENTE_ENGENHARIA",
    "engineering manager": "GERENTE_ENGENHARIA",
    "head of engineering": "GERENTE_ENGENHARIA",
    "engineering director": "GERENTE_ENGENHARIA",
    "diretor de engenharia": "GERENTE_ENGENHARIA",
    "vp of engineering": "GERENTE_ENGENHARIA",
    "general engineering manager": "GERENTE_ENGENHARIA",
    "engineering coordinator": "GERENTE_ENGENHARIA",
    "coordenador de engenharia": "GERENTE_ENGENHARIA",

    # GERENTE_INDUSTRIAL (10)
    "gerente industrial": "GERENTE_INDUSTRIAL",
    "plant manager": "GERENTE_INDUSTRIAL",
    "industrial director": "GERENTE_INDUSTRIAL",
    "plant director": "GERENTE_INDUSTRIAL",
    "diretor industrial": "GERENTE_INDUSTRIAL",
    "operations manager": "GERENTE_INDUSTRIAL",
    "gerente de operacoes": "GERENTE_INDUSTRIAL",
    "operations director": "GERENTE_INDUSTRIAL",
    "diretor de operacoes": "GERENTE_INDUSTRIAL",
    "site director": "GERENTE_INDUSTRIAL",

    # COORDENADOR_OBRAS (8)
    "coordenador de obras": "COORDENADOR_OBRAS",
    "construction coordinator": "COORDENADOR_OBRAS",
    "site coordinator": "COORDENADOR_OBRAS",
    "gerente de obras": "COORDENADOR_OBRAS",
    "construction manager": "COORDENADOR_OBRAS",
    "site manager": "COORDENADOR_OBRAS",
    "construction director": "COORDENADOR_OBRAS",
    "diretor de obras": "COORDENADOR_OBRAS",

    # COORDENADOR_MANUTENCAO (9)
    "coordenador de manutencao": "COORDENADOR_MANUTENCAO",
    "maintenance coordinator": "COORDENADOR_MANUTENCAO",
    "gerente de manutencao": "COORDENADOR_MANUTENCAO",
    "maintenance manager": "COORDENADOR_MANUTENCAO",
    "maintenance director": "COORDENADOR_MANUTENCAO",
    "diretor de manutencao": "COORDENADOR_MANUTENCAO",
    "reliability engineer": "COORDENADOR_MANUTENCAO",
    "engenheiro de manutencao": "COORDENADOR_MANUTENCAO",
    "maintenance engineer": "COORDENADOR_MANUTENCAO",

    # ENGENHEIRO_MECANICO_CIVIL (17)
    "engenheiro civil": "ENGENHEIRO_MECANICO_CIVIL",
    "civil engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro mecanico": "ENGENHEIRO_MECANICO_CIVIL",
    "mechanical engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro eletricista": "ENGENHEIRO_MECANICO_CIVIL",
    "electrical engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro de automacao": "ENGENHEIRO_MECANICO_CIVIL",
    "automation engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro de processos": "ENGENHEIRO_MECANICO_CIVIL",
    "process engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "senior engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro senior": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro pleno": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro de planejamento": "ENGENHEIRO_MECANICO_CIVIL",
    "planning engineer": "ENGENHEIRO_MECANICO_CIVIL",
    "engenheiro de projetos": "ENGENHEIRO_MECANICO_CIVIL",
    "project engineer": "ENGENHEIRO_MECANICO_CIVIL",

    # PROJETISTA (4)
    "projetista": "PROJETISTA",
    "designer": "PROJETISTA",
    "drafter": "PROJETISTA",
    "cad designer": "PROJETISTA",

    # OUTRO - C-level + outros (Anderson confirmou: C-level NAO atende
    # procurement em megaprojetos, mas mantemos pra audit/social proof)
    "ceo": "OUTRO",
    "chief executive officer": "OUTRO",
    "president": "OUTRO",
    "cfo": "OUTRO",
    "chief financial officer": "OUTRO",
    "coo": "OUTRO",
    "chief operating officer": "OUTRO",
    "cto": "OUTRO",
    "chief technology officer": "OUTRO",
    "diretor presidente": "OUTRO",
    "country manager": "OUTRO",
    "diretor de infraestrutura": "OUTRO",
    "infrastructure director": "OUTRO",
    "conselheiro": "OUTRO",
}

TERMOS_BUSCA = list(CARGO_MAPPING_PTBR_EN.keys())

# Bucket de queries (search engines aceitam ~10 OR por query)
QUERY_BUCKETS = [TERMOS_BUSCA[i:i+10] for i in range(0, len(TERMOS_BUSCA), 10)]

NIVEL_POR_TIPO = {
    "GERENTE_SUPRIMENTOS": "tatico",
    "GERENTE_COMPRAS": "tatico",
    "SUPPLY_CHAIN": "tatico",
    "GERENTE_PROJETOS": "tatico",
    "GERENTE_ENGENHARIA": "tatico",
    "GERENTE_INDUSTRIAL": "tatico",
    "COORDENADOR_OBRAS": "tatico",
    "COORDENADOR_MANUTENCAO": "tatico",
    "ENGENHEIRO_MECANICO_CIVIL": "operacional",
    "PROJETISTA": "operacional",
    "OUTRO": "estrategico",
}


def normalizar_cargo(cargo_raw: str) -> Optional[str]:
    """Retorna tipo_cargo canonico ou None se nao reconhecido (vai pra OUTRO)."""
    if not cargo_raw:
        return None
    cargo_lower = unidecode(cargo_raw.lower().strip())
    # tentar match direto
    if cargo_lower in CARGO_MAPPING_PTBR_EN:
        return CARGO_MAPPING_PTBR_EN[cargo_lower]
    # tentar substring (para cargos longos como "Engineering Manager - Industrial")
    for termo, tipo in CARGO_MAPPING_PTBR_EN.items():
        if termo in cargo_lower:
            return tipo
    return None


def detectar_idioma(cargo_raw: str) -> str:
    """Detecta pt-br vs en. Default pt-br."""
    if not cargo_raw:
        return "pt-br"
    cargo_lower = cargo_raw.lower()
    palavras_pt = ["gerente", "diretor", "engenheiro", "coordenador", "compras",
                   "suprimentos", "manutencao", "manutenção", "obras"]
    palavras_en = ["manager", "director", "engineer", "coordinator", "head",
                   "chief", "officer", "supply", "purchasing", "buyer"]
    pt_count = sum(1 for p in palavras_pt if p in cargo_lower)
    en_count = sum(1 for p in palavras_en if p in cargo_lower)
    if en_count > pt_count:
        return "en"
    return "pt-br"


def determinar_nivel(tipo_cargo: Optional[str]) -> Optional[str]:
    if not tipo_cargo:
        return None
    return NIVEL_POR_TIPO.get(tipo_cargo)
