"""Reproduz o bug #1 (downgrade silencioso no webhook MP) no nível da decisão.

Antes do Patch B, o webhook fazia UPDATE prestadores SET plano=<plano pago>
incondicionalmente. Um PROFISSIONAL ativo que pagasse um ESSENCIAL era
rebaixado em silêncio. eh_downgrade() é a guarda que passa a barrar isso.
"""
from planos import eh_downgrade, rank_plano


def test_rank_ordem():
    assert rank_plano("GRATUITO") < rank_plano("ESSENCIAL") < rank_plano("PROFISSIONAL")


def test_rank_aliases_legacy():
    assert rank_plano("STANDARD") == rank_plano("ESSENCIAL")
    assert rank_plano("PREMIUM") == rank_plano("PROFISSIONAL")


def test_rank_desconhecido_e_none():
    assert rank_plano(None) == 0
    assert rank_plano("") == 0
    assert rank_plano("xpto") == 0


def test_bug_profissional_paga_essencial_eh_downgrade():
    # O caso exato do bug: PROFISSIONAL ativo paga ESSENCIAL.
    assert eh_downgrade("ESSENCIAL", "PROFISSIONAL", plano_ativo=True) is True
    # Mesma coisa com os aliases legacy gravados em pagamentos antigos.
    assert eh_downgrade("STANDARD", "PREMIUM", plano_ativo=True) is True


def test_upgrade_nao_eh_downgrade():
    assert eh_downgrade("PROFISSIONAL", "ESSENCIAL", plano_ativo=True) is False


def test_mesmo_tier_nao_eh_downgrade():
    # Renovação no mesmo plano deve seguir o fluxo normal (não bloqueia).
    assert eh_downgrade("ESSENCIAL", "ESSENCIAL", plano_ativo=True) is False


def test_plano_expirado_permite_assinar_menor():
    # Se o tier maior já expirou, pagar um menor é reassinatura legítima.
    assert eh_downgrade("ESSENCIAL", "PROFISSIONAL", plano_ativo=False) is False


def test_sem_plano_atual_ou_gratuito_nunca_bloqueia():
    assert eh_downgrade("ESSENCIAL", None, plano_ativo=True) is False
    assert eh_downgrade("ESSENCIAL", "GRATUITO", plano_ativo=True) is False
