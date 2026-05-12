"""
Lista de cargos decisores reais para prospecção B2B em megaprojetos.

Validada por Anderson (sócio comercial) em 2026-04-30: estes são os cargos
*operacionais/gerenciais* que de fato decidem compras e definem fornecedores
em obras de capex bilionário. C-level (Diretor-Presidente, CEO, etc.) NÃO
costuma atender chamada de procurement — quem atende é gerência média.

Importado por:
- orchestrator.py (futuras buscas automatizadas)
- scripts manuais de pesquisa de decisores
- queries que filtram decisores_obra.cargo
"""

# Cargos decisores priorizados (em ordem de impacto típico em decisão de compra)
CARGOS_DECISORES: list[str] = [
    # Compras e Suprimentos — decisor principal de procurement
    "Gerente de Compras",
    "Coordenador de Compras",
    "Gerente de Suprimentos",
    "Coordenador de Suprimentos",
    "Supply Chain Manager",
    # Engenharia técnica — define especificações que viram demanda
    "Engenheiro Mecânico",
    "Engenheiro Civil",
    "Gerente de Engenharia",
    "Engenheiro de Projetos",
    "Projetista",
    # Operação e manutenção — demanda recorrente de OPEX
    "Coordenador de Manutenção",
    "Gerente Industrial",
    "Coordenador de Obras",
    "Gerente de Projetos",
]

# Termos OR para WebSearch (versão minúscula, sem cargo + variações comuns)
TERMOS_BUSCA_WEBSEARCH: list[str] = [
    "gerente de suprimentos",
    "coordenador de compras",
    "engenheiro de projetos",
    "supply chain",
    "coordenador de obras",
    "gerente industrial",
    "gerente de engenharia",
    "engenheiro civil",
    "engenheiro mecânico",
    "coordenador de manutenção",
    "projetista",
]

# Sites onde costumam aparecer perfis com cargo + empresa identificáveis
SITES_PRIORITARIOS: list[str] = [
    "linkedin.com",
    "escavador.com",
    "economatica.com",
]


def montar_query_websearch(empresa: str, uf: str | None = None) -> str:
    """
    Constrói query no padrão validado:
    \"<empresa>\" \"<UF>\" (cargo OR cargo OR ...) site:linkedin.com OR site:escavador.com

    Args:
        empresa: nome da empresa (será aspeado).
        uf: sigla da UF (opcional, ajuda a desambiguar).

    Returns:
        String pronta pra WebSearch.
    """
    cargos_or = " OR ".join(TERMOS_BUSCA_WEBSEARCH)
    sites_or = " OR ".join(f"site:{s}" for s in SITES_PRIORITARIOS)
    uf_part = f' "{uf}"' if uf else ""
    return f'"{empresa}"{uf_part} ({cargos_or}) ({sites_or})'


# Palavras-chave decisoras (substring match, lowercase, sem acento) — usadas
# pela classificação Obra-Ouro: nivel1_nome + (email|linkedin) + cargo casa
# alguma destas. Espelhada em SQL pela função cargo_decisor_keyword(text)
# e em Python por main._cargo_e_decisor(cargo). Manter os três em sincronia.
CARGO_KEYWORDS_OURO: tuple[str, ...] = (
    "compras", "suprimentos", "supply", "procurement", "sourcing",
    "engenh", "projetos", "obras", "manutencao", "industrial",
    "coordenador", "gerente", "gestor",
)

# Mapeamento cargo → categoria pra ordenação/priorização em UI
CATEGORIA_DO_CARGO: dict[str, str] = {
    "Gerente de Compras":         "PROCUREMENT",
    "Coordenador de Compras":     "PROCUREMENT",
    "Gerente de Suprimentos":     "PROCUREMENT",
    "Coordenador de Suprimentos": "PROCUREMENT",
    "Supply Chain Manager":       "PROCUREMENT",
    "Engenheiro Mecânico":        "ENGENHARIA_TECNICA",
    "Engenheiro Civil":           "ENGENHARIA_TECNICA",
    "Gerente de Engenharia":      "ENGENHARIA_TECNICA",
    "Engenheiro de Projetos":     "ENGENHARIA_TECNICA",
    "Projetista":                 "ENGENHARIA_TECNICA",
    "Coordenador de Manutenção":  "OPERACAO_OPEX",
    "Gerente Industrial":         "OPERACAO_OPEX",
    "Coordenador de Obras":       "OPERACAO_OPEX",
    "Gerente de Projetos":        "OPERACAO_OPEX",
}
