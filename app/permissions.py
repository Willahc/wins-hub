"""Permissões centralizadas (representante × cliente).

Regra única e central:
- is_representante = TRUE  → vê tudo EXCETO decisores de obra
- is_representante = FALSE → acesso controlado por plano (GRATUITO/STANDARD/PREMIUM)
"""

ADMIN_EMAIL = "williamvnvn@gmail.com"
PLANOS_PAGOS = ("STANDARD", "PREMIUM")


def pode_ver_decisores_obra(user: dict) -> bool:
    """Decisores de obra: clientes pagantes (não reps). Admin sempre vê."""
    if not user:
        return False
    if eh_admin(user):
        return True
    if user.get("is_representante"):
        return False
    return (user.get("plano") or "").upper() in PLANOS_PAGOS


def pode_ver_conteudo_pago(user: dict) -> bool:
    """Tudo o resto da plataforma: clientes pagantes OU representantes."""
    if not user:
        return False
    if user.get("is_representante"):
        return True
    return (user.get("plano") or "").upper() in PLANOS_PAGOS


def pode_acessar_painel_vendas(user: dict) -> bool:
    """Painel /vendas: só representantes."""
    return bool(user and user.get("is_representante"))


def eh_admin(user: dict) -> bool:
    """Admin (/admin-vendas, ações privilegiadas): só email do owner."""
    return bool(user and user.get("email") == ADMIN_EMAIL)
