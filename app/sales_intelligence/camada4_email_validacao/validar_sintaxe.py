"""Validacao de sintaxe de email (RFC 5322 simplificado, suficiente p/ B2B)."""
import re
from typing import Tuple

# regex pragmatica: nao e RFC 5322 completo mas cobre 99% dos B2B
EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)+)$"
)


def validar_sintaxe(email: str) -> Tuple[bool, str]:
    """Retorna (ok, dominio). Domain vazio se sintaxe invalida."""
    if not email or len(email) > 254:
        return False, ""
    email = email.strip().lower()
    m = EMAIL_RE.match(email)
    if not m:
        return False, ""
    dominio = m.group(1).lower()
    # local-part sem espacos, sem caracteres reservados
    local = email.split("@", 1)[0]
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return False, ""
    return True, dominio
