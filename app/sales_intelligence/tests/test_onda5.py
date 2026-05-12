"""Testes Onda 5: cache routing, parser blacklist, integracao C3->C4."""
import sys
from pathlib import Path
from unittest.mock import patch
from datetime import date, timedelta

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada3_decisores.linkedin_search import (
    _validar_nome, BLACKLIST_NOMES,
)
from sales_intelligence.models.decisor import Decisor
from sales_intelligence.models.email_validacao import EmailValidacao


# ---------- parser blacklist ----------
def test_blacklist_rejeita_termo_comercial_isolado():
    assert _validar_nome("Sourcing", "Petrobras") is False
    assert _validar_nome("supply chain", "Petrobras") is False
    assert _validar_nome("Manager", "Petrobras") is False


def test_blacklist_rejeita_primeira_palavra_comercial():
    assert _validar_nome("Sourcing Specialist", "Petrobras") is False
    assert _validar_nome("Engineering Department", "Petrobras") is False
    assert _validar_nome("Grupo Eco", "Ecorodovias") is False


def test_blacklist_rejeita_nome_com_empresa():
    assert _validar_nome("Petrobras Brasil", "Petrobras") is False
    assert _validar_nome("CCR ViaSul team", "CCR ViaSul") is False


def test_blacklist_aceita_nome_real():
    assert _validar_nome("Carlos Lima", "Ecorodovias") is True
    assert _validar_nome("Jorgen Poulson", "Petrobras") is True
    assert _validar_nome("Joao da Silva Pereira", "X Corp") is True


def test_blacklist_rejeita_nome_curto():
    assert _validar_nome("Joao", "X") is False
    assert _validar_nome("", "X") is False


def test_blacklist_rejeita_nome_muito_longo():
    longo = "A B C D E F G"  # 7 palavras
    assert _validar_nome(longo, "X") is False


def test_blacklist_rejeita_nome_com_digito():
    assert _validar_nome("Joao Silva 2024", "X") is False
    assert _validar_nome("Test123 User", "X") is False


# ---------- integracao C3 -> C4 ----------
def test_integracao_hunter_hit_alta_score():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    d = Decisor(cnpj="12345678000195", nome_pessoa="Joao Silva",
                cargo_raw="Manager", tipo_cargo="GERENTE_ENGENHARIA",
                confianca="alta", fonte_descoberta="serper",
                snippet_origem="...", url_origem="...",
                revalidacao=date.today() + timedelta(days=180))
    hunter_fake = {"email": "joao.silva@x.com", "score": 92}
    fake_val = EmailValidacao(email="joao.silva@x.com", status="verified_smtp",
                               sintaxe_ok=True, fonte_validacao="hunter:valid",
                               confianca="alta")
    with patch("sales_intelligence.integracao_c3_c4.hunter_find", return_value=hunter_fake):
        with patch("sales_intelligence.integracao_c3_c4.validar_email", return_value=fake_val):
            with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                out = enriquecer_decisores_com_email("12345678000195", "x.com", [d])
    assert len(out) == 1
    assert out[0].email == "joao.silva@x.com"
    assert out[0].email_status == "verified_smtp"


def test_integracao_hunter_score_baixo_recusa():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    d = Decisor(cnpj="12345678000195", nome_pessoa="Joao Silva",
                cargo_raw="Manager", tipo_cargo="OUTRO", confianca="media",
                fonte_descoberta="serper", snippet_origem="...", url_origem="...",
                revalidacao=date.today() + timedelta(days=180))
    hunter_fake = {"email": "joao@x.com", "score": 45}  # baixo
    # sem pattern -> nao gera email
    with patch("sales_intelligence.integracao_c3_c4.hunter_find", return_value=hunter_fake):
        with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
            out = enriquecer_decisores_com_email("12345678000195", "x.com", [d], pattern=None)
    assert out[0].email is None
    assert out[0].email_status is None


def test_integracao_pattern_fallback():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    from sales_intelligence.models.email_pattern import EmailPattern
    pat = EmailPattern(padrao="{first}.{last}", confianca="alta",
                       exemplos=["a.b@x.com"], amostra_total=4, dominio_origem="x.com")
    d = Decisor(cnpj="12345678000195", nome_pessoa="Maria Santos",
                cargo_raw="Buyer", tipo_cargo="GERENTE_COMPRAS", confianca="alta",
                fonte_descoberta="serper", snippet_origem="...", url_origem="...",
                revalidacao=date.today() + timedelta(days=180))
    fake_val = EmailValidacao(email="maria.santos@x.com", status="verified_smtp",
                               sintaxe_ok=True, fonte_validacao="smtp", confianca="alta")
    with patch("sales_intelligence.integracao_c3_c4.hunter_find", return_value=None):
        with patch("sales_intelligence.integracao_c3_c4.validar_email", return_value=fake_val):
            with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
                out = enriquecer_decisores_com_email("12345678000195", "x.com", [d], pattern=pat)
    assert out[0].email == "maria.santos@x.com"
    assert out[0].email_status == "verified_smtp"


def test_integracao_pattern_baixa_confianca_recusa():
    from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
    from sales_intelligence.models.email_pattern import EmailPattern
    pat = EmailPattern(padrao="{first}.{last}", confianca="baixa",
                       exemplos=["a.b@x.com"], amostra_total=1, dominio_origem="x.com")
    d = Decisor(cnpj="12345678000195", nome_pessoa="X Y",
                cargo_raw="?", tipo_cargo="OUTRO", confianca="baixa",
                fonte_descoberta="serper", snippet_origem="...", url_origem="...",
                revalidacao=date.today() + timedelta(days=180))
    with patch("sales_intelligence.integracao_c3_c4.hunter_find", return_value=None):
        with patch("sales_intelligence.db.cache_decisores.gravar_decisor", return_value=True):
            out = enriquecer_decisores_com_email("12345678000195", "x.com", [d], pattern=pat)
    assert out[0].email is None  # anti-alucinacao: pattern baixa nao gera


# ---------- cache routing fix ----------
def test_get_db_host_resolve_loopback():
    """Apos F1.3 (compose dev), get_db_host deve achar 127.0.0.1 ou db."""
    import sales_intelligence.db as db_pkg
    # forcar re-resolucao
    db_pkg._RESOLVED_HOST = None
    host = db_pkg.get_db_host()
    assert host in ("db", "127.0.0.1"), f"Expected db/127.0.0.1, got {host}"
