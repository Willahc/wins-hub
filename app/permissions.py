"""Permissões centralizadas (representante × cliente × co-admin).

Regra única e central:
- eh_admin           = owner (email == ADMIN_EMAIL) → tudo, incluindo painel /admin
- eh_co_admin        = admin restrito → ações comerciais (enriquecer/match/pdf) + decisores,
                       SEM painel /admin e SEM endpoints destrutivos (criar-rep/comissão/matchmaker)
- is_representante   = TRUE (não co-admin) → vê tudo EXCETO decisores de obra
- is_representante   = FALSE → controlado por plano (GRATUITO/STANDARD/PREMIUM)
"""

ADMIN_EMAIL = "williamvnvn@gmail.com"
PLANOS_PAGOS = ("STANDARD", "PREMIUM")


def pode_ver_decisores_obra(user: dict) -> bool:
    """Decisores de obra: admin, co-admin ou cliente pagante. Reps puros NÃO veem."""
    if not user:
        return False
    if eh_admin(user):
        return True
    if eh_co_admin(user):
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


def eh_co_admin(user: dict) -> bool:
    """Co-admin: ações comerciais (enriquecer/match/pdf + decisor) sem painel admin."""
    return bool(user and user.get("eh_co_admin"))


def eh_admin_ou_co(user: dict) -> bool:
    return eh_admin(user) or eh_co_admin(user)
