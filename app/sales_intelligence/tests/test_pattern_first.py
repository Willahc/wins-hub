"""Testes pattern-first strategy (integracao C3->C4 reescrita)."""
import sys
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.models.decisor import Decisor
from sales_intelligence.models.email_pattern import EmailPattern
from sales_intelligence.models.email_validacao import EmailValidacao


def _make_decisor(nome="Joao Silva"):
    return Decisor(
        cnpj="12345678000195", nome_pessoa=nome,
        cargo_raw="Manager", tipo_cargo="GERENTE_ENGENHARIA",
        cargo_idioma="en", cargo_nivel="tatico",
        confianca="alta", fonte_descoberta="serper",
        snippet_origem="...", url_origem="...",
        revalidacao=date.today() + timedelta(days=180),
    )


def _make_pattern(confianca="alta", padrao="{first}.{last}"):
    return EmailPattern(
        padrao=padrao, confianca=confianca,
        exemplos=["a.b@x.com", "c.d@x.com"], amostra_total=4,
        dominio_origem="x.com",
    )


# --------- happy path: pattern + SMTP decisivo (sem Hunter) ---------
def test_pattern_first_smtp_verified_nao_chama_hunter():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    fake_val = EmailValidacao(
        email="joao.silva@x.com", status="verified_smtp",
        sintaxe_ok=True, fonte_validacao="smtp", confianca="alta",
    )
    pat = _make_pattern()
    with patch("sales_intelligence.integracao_c3_c4.hunter_find") as mock_h:
        with patch("sales_intelligence.integracao_c3_c4.validar_email", return_value=fake_val):
            with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                      [_make_decisor()], pattern=pat)
    assert out[0].email == "joao.silva@x.com"
    assert out[0].email_status == "verified_smtp"
    mock_h.assert_not_called()  # Hunter NAO foi chamado


def test_pattern_first_smtp_invalid_nao_chama_hunter():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    fake_val = EmailValidacao(
        email="joao.silva@x.com", status="invalid",
        sintaxe_ok=True, fonte_validacao="smtp", confianca="alta",
    )
    pat = _make_pattern()
    with patch("sales_intelligence.integracao_c3_c4.hunter_find") as mock_h:
        with patch("sales_intelligence.integracao_c3_c4.validar_email", return_value=fake_val):
            with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                      [_make_decisor()], pattern=pat)
    assert out[0].email_status == "invalid"
    mock_h.assert_not_called()


# --------- SMTP inconclusivo -> Hunter tiebreaker ---------
def test_pattern_first_smtp_greylisted_chama_hunter_fallback():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    fake_grey = EmailValidacao(email="joao.silva@x.com", status="greylisted",
                                sintaxe_ok=True, fonte_validacao="smtp", confianca="baixa")
    fake_hunter_resolve = EmailValidacao(email="joao.silva@x.com", status="verified_smtp",
                                          sintaxe_ok=True, fonte_validacao="hunter:valid",
                                          confianca="alta")
    pat = _make_pattern()

    call_count = {"n": 0}
    def fake_validar(email, usar_hunter_fallback=False):
        call_count["n"] += 1
        # 1a chamada: SMTP only -> greylisted
        # 2a chamada: com Hunter fallback -> resolve
        return fake_hunter_resolve if usar_hunter_fallback else fake_grey

    with patch("sales_intelligence.integracao_c3_c4.validar_email", side_effect=fake_validar):
        with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
            out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                  [_make_decisor()], pattern=pat)
    assert out[0].email_status == "verified_smtp"  # Hunter resolveu
    assert call_count["n"] == 2  # SMTP solo + SMTP+Hunter


# --------- Sem pattern -> Hunter finder ---------
def test_sem_pattern_chama_hunter_finder():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    fake_val = EmailValidacao(email="joao@x.com", status="verified_smtp",
                               sintaxe_ok=True, fonte_validacao="smtp", confianca="alta")
    hunter_hit = {"email": "joao@x.com", "score": 85}

    with patch("sales_intelligence.integracao_c3_c4.resolver_pattern_para_dominio",
                return_value=None):
        with patch("sales_intelligence.integracao_c3_c4.hunter_find",
                    return_value=hunter_hit) as mock_h:
            with patch("sales_intelligence.integracao_c3_c4.validar_email", return_value=fake_val):
                with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                    out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                          [_make_decisor()])
    assert out[0].email == "joao@x.com"
    assert out[0].email_status == "verified_smtp"
    mock_h.assert_called_once()


# --------- Pattern baixa confianca -> pula direto pra Hunter ---------
def test_pattern_baixa_confianca_pula_pra_hunter():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    pat_fraco = _make_pattern(confianca="baixa")
    hunter_hit = {"email": "joao@x.com", "score": 85}
    fake_val = EmailValidacao(email="joao@x.com", status="verified_smtp",
                               sintaxe_ok=True, fonte_validacao="smtp", confianca="alta")

    with patch("sales_intelligence.integracao_c3_c4.hunter_find",
                return_value=hunter_hit) as mock_h:
        with patch("sales_intelligence.integracao_c3_c4.validar_email", return_value=fake_val):
            with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                      [_make_decisor()], pattern=pat_fraco)
    assert out[0].email == "joao@x.com"
    mock_h.assert_called_once()


# --------- Sem pattern + Hunter desligado -> email=None ---------
def test_sem_pattern_sem_hunter_email_none():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    with patch("sales_intelligence.integracao_c3_c4.resolver_pattern_para_dominio",
                return_value=None):
        with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
            out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                  [_make_decisor()], permitir_hunter=False)
    assert out[0].email is None
    assert out[0].email_status is None


# --------- Hunter score baixo recusa ---------
def test_hunter_finder_score_baixo_recusa():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    with patch("sales_intelligence.integracao_c3_c4.resolver_pattern_para_dominio",
                return_value=None):
        with patch("sales_intelligence.integracao_c3_c4.hunter_find",
                    return_value={"email": "x@x.com", "score": 40}):
            with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                out = enriquecer_decisores_com_email("12345678000195", "x.com",
                                                      [_make_decisor()])
    assert out[0].email is None  # Hunter abaixo de 70 nao confiar


# --------- Resolver pattern usa cache ---------
def test_resolver_pattern_usa_cache():
    from sales_intelligence.camada2_pattern_detection.resolver_pattern import resolver_pattern_para_dominio
    pat_cached = _make_pattern()
    with patch("sales_intelligence.db.cache_pattern.buscar_pattern_cache",
                return_value=pat_cached):
        out = resolver_pattern_para_dominio("x.com")
    assert out is not None
    assert out.padrao == "{first}.{last}"
