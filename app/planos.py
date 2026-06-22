"""Hierarquia canônica de planos do WiNS Hub.

Isolado em módulo próprio pra ser testável sem subir o app inteiro
(main.py conecta no banco já no import). Usado pela guarda anti-downgrade
do webhook do Mercado Pago (Patch B / bug #1).

Tiers: GRATUITO(legado) < SETOR < NACIONAL < ENTERPRISE.
Aliases legacy de pagamentos antigos: STANDARD->ESSENCIAL, PREMIUM->PROFISSIONAL.
"""

_PLANO_RANK = {"GRATUITO": 0, "SETOR": 1, "NACIONAL": 2, "ENTERPRISE": 3}
_PLANO_ALIAS = {"STANDARD": "SETOR", "ESSENCIAL": "SETOR", "PREMIUM": "NACIONAL", "PROFISSIONAL": "NACIONAL"}


def rank_plano(plano: str) -> int:
    """Rank canônico do plano (0=GRATUITO .. 2=PROFISSIONAL).

    Plano None/desconhecido -> 0 (trata como base, nunca bloqueia upgrade).
    """
    p = (plano or "").upper()
    return _PLANO_RANK.get(_PLANO_ALIAS.get(p, p), 0)


def eh_downgrade(plano_pago: str, plano_atual: str, plano_ativo: bool) -> bool:
    """True se aplicar `plano_pago` rebaixaria um `plano_atual` ainda ativo.

    Regra comercial (definida por William 2026-06-17): pagamento de um plano
    de tier menor que o ativo NÃO deve alterar o prestador — apenas alertar.
    """
    return bool(plano_ativo) and rank_plano(plano_pago) < rank_plano(plano_atual)
