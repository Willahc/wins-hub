"""
Helper de filtragem de decisores por palavras-chave de cargo (lista do Anderson).

Normaliza cargo (lowercase + remove acentos + remove pontuação) e checa se contém
alguma keyword da lista CARGOS_KEYWORDS.
"""
import unicodedata
import re

CARGOS_KEYWORDS = [
    # Compras / suprimentos / sourcing
    "compras", "procurement", "purchasing", "buying",
    "suprimentos", "supply chain", "sourcing",
    # Engenharia
    "engenheiro", "engenheira", "engineer", "engineering",
    "mecanica", "mecanico", "mechanical",
    "civil",
    "manutencao", "maintenance",
    "obras", "construction", "site manager",
    "projetista", "designer",
    "industrial",
    "projetos", "project",
    # Gerência / coordenação genéricas (cobre HR Manager, Operations Manager etc.)
    "gerente", "manager", "coordenador", "coordinator",
    # Diretoria genérica (validado empiricamente 2026-05-08: 10 Directors no DB sem outra keyword)
    "director", "diretor", "diretora",
    # C-level
    "ceo", "cfo", "coo", "cto", "cio",
    "presidente", "president",
    "vice-presidente", "vice president", "vice-president", "vp",
    # Comercial / sales (decisores de B2B de obras)
    "comercial", "commercial",
]


def _normalizar(texto):
    if not texto:
        return ""
    s = unicodedata.normalize("NFD", str(texto))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _matches_keyword(cargo_norm: str, kw_norm: str) -> bool:
    """Casa keyword no cargo. Para siglas curtas (<=3 chars como 'cto', 'ceo'),
    exige word-boundary pra evitar substring acidental ('cto' em 'director')."""
    if not kw_norm:
        return False
    if len(kw_norm) <= 3:
        return bool(re.search(r"\b" + re.escape(kw_norm) + r"\b", cargo_norm))
    return kw_norm in cargo_norm


def filtrar_por_cargo_decisor(items, campo_cargo="position"):
    """Filtra lista de dicts mantendo só items cujo cargo (campo_cargo) bate
    com alguma keyword.

    Args:
        items: lista de dicts
        campo_cargo: nome da chave do dict que tem o cargo

    Returns:
        (items_match, items_descartados, palavras_que_bateram_set)
    """
    match = []
    descartados = []
    palavras_que_bateram = set()

    keywords_norm = [(_normalizar(k), k) for k in CARGOS_KEYWORDS]

    for item in items or []:
        cargo = item.get(campo_cargo) if isinstance(item, dict) else None
        cargo_norm = _normalizar(cargo)
        bateu = False
        if cargo_norm:
            for kw_norm, kw_orig in keywords_norm:
                if _matches_keyword(cargo_norm, kw_norm):
                    palavras_que_bateram.add(kw_orig)
                    bateu = True
                    break
        if bateu:
            match.append(item)
        else:
            descartados.append(item)

    return match, descartados, sorted(palavras_que_bateram)
