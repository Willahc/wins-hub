import re
from typing import Optional
from unidecode import unidecode


def _normalizar_nome(nome: str) -> tuple:
    """Retorna (first, last) em lowercase ASCII."""
    if not nome:
        return ("", "")
    s = unidecode(nome).lower().strip()
    s = re.sub(r"[^a-z\s]", " ", s)
    partes = [p for p in s.split() if p]
    if not partes:
        return ("", "")
    if len(partes) == 1:
        return (partes[0], "")
    return (partes[0], partes[-1])


def gerar_email(nome_completo: str, padrao, dominio: Optional[str] = None) -> Optional[str]:
    """Aplica padrao ao nome. Recusa se confianca='baixa' (anti-alucinacao)."""
    if padrao is None:
        return None
    if hasattr(padrao, "confianca") and padrao.confianca == "baixa":
        return None

    pat_str = padrao.padrao if hasattr(padrao, "padrao") else str(padrao)
    dom = dominio or (padrao.dominio_origem if hasattr(padrao, "dominio_origem") else None)
    if not dom:
        return None

    first, last = _normalizar_nome(nome_completo)
    if not first:
        return None

    f = first[0] if first else ""
    l_ini = last[0] if last else ""

    local = (pat_str
             .replace("{first}", first)
             .replace("{last}", last)
             .replace("{f}", f)
             .replace("{l}", l_ini))
    # se padrao exige last mas nao temos, recusa
    if "{last}" in pat_str and not last:
        return None
    local = re.sub(r"[^a-z0-9._-]", "", local)
    if not local:
        return None
    return f"{local}@{dom}"
