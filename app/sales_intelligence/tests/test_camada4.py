import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada4_email_validacao.validar_sintaxe import validar_sintaxe
from sales_intelligence.camada4_email_validacao import (
    validar_smtp, hunter_adapter, orquestrador,
)
from sales_intelligence.models.email_validacao import EmailValidacao


# -------- sintaxe --------
def test_sintaxe_valida():
    ok, dom = validar_sintaxe("joao.silva@empresa.com.br")
    assert ok is True
    assert dom == "empresa.com.br"


def test_sintaxe_lowercase():
    ok, dom = validar_sintaxe("JOAO@EMPRESA.COM")
    assert ok is True
    assert dom == "empresa.com"


def test_sintaxe_invalida_sem_arroba():
    assert validar_sintaxe("nope") == (False, "")


def test_sintaxe_invalida_dominio_curto():
    assert validar_sintaxe("a@b") == (False, "")


def test_sintaxe_local_part_invalida():
    assert validar_sintaxe(".joao@x.com") == (False, "")
    assert validar_sintaxe("joao..silva@x.com") == (False, "")
    assert validar_sintaxe("joao@x.com.") == (False, "")


def test_sintaxe_email_muito_longo():
    longo = "a" * 250 + "@x.com"
    assert validar_sintaxe(longo) == (False, "")


# -------- hunter mapping --------
def test_hunter_status_map_valid():
    assert hunter_adapter.mapear_status_hunter("valid") == "verified_smtp"
    assert hunter_adapter.mapear_status_hunter("invalid") == "invalid"
    assert hunter_adapter.mapear_status_hunter("accept_all") == "catch_all"
    assert hunter_adapter.mapear_status_hunter("disposable") == "invalid"
    assert hunter_adapter.mapear_status_hunter("webmail") == "verified_mx"
    assert hunter_adapter.mapear_status_hunter("unknown") == "greylisted"
    assert hunter_adapter.mapear_status_hunter("xyz") == "greylisted"


# -------- orquestrador --------
def test_orquestrador_sintaxe_falha():
    v = orquestrador.validar_email("malformado", usar_hunter_fallback=False)
    assert v.status == "invalid"
    assert v.fonte_validacao == "sintaxe"
    assert v.sintaxe_ok is False


def test_orquestrador_dominio_sem_mx():
    with patch.object(orquestrador, "tem_mx", return_value=(False, None)):
        v = orquestrador.validar_email("a@dominio-inexistente-zzz.zzz",
                                        usar_hunter_fallback=False)
    assert v.status == "invalid"
    assert v.fonte_validacao == "mx"


def test_orquestrador_smtp_verified():
    with patch.object(orquestrador, "tem_mx", return_value=(True, "mx.x.com")):
        with patch.object(orquestrador, "is_catch_all", return_value=False):
            with patch.object(orquestrador, "smtp_probe",
                              return_value=("verified_smtp", 250, "OK")):
                v = orquestrador.validar_email("joao@x.com", usar_hunter_fallback=False)
    assert v.status == "verified_smtp"
    assert v.confianca == "alta"
    assert v.smtp_response_code == 250


def test_orquestrador_smtp_invalid():
    with patch.object(orquestrador, "tem_mx", return_value=(True, "mx.x.com")):
        with patch.object(orquestrador, "is_catch_all", return_value=False):
            with patch.object(orquestrador, "smtp_probe",
                              return_value=("invalid", 550, "User unknown")):
                v = orquestrador.validar_email("notexist@x.com", usar_hunter_fallback=False)
    assert v.status == "invalid"
    assert v.smtp_response_code == 550


def test_orquestrador_catch_all_sem_hunter():
    with patch.object(orquestrador, "tem_mx", return_value=(True, "mx.x.com")):
        with patch.object(orquestrador, "is_catch_all", return_value=True):
            v = orquestrador.validar_email("a@x.com", usar_hunter_fallback=False)
    assert v.status == "catch_all"
    assert v.catch_all is True
    assert v.confianca == "baixa"


def test_orquestrador_greylisted_sem_hunter():
    with patch.object(orquestrador, "tem_mx", return_value=(True, "mx.x.com")):
        with patch.object(orquestrador, "is_catch_all", return_value=False):
            with patch.object(orquestrador, "smtp_probe",
                              return_value=("greylisted", None, "timeout")):
                v = orquestrador.validar_email("a@x.com", usar_hunter_fallback=False)
    assert v.status == "greylisted"
    assert v.confianca == "baixa"


def test_orquestrador_hunter_fallback_em_greylist():
    fake_hunter_data = {"result": "valid", "score": 95}
    with patch.object(orquestrador, "tem_mx", return_value=(True, "mx.x.com")):
        with patch.object(orquestrador, "is_catch_all", return_value=False):
            with patch.object(orquestrador, "smtp_probe",
                              return_value=("greylisted", None, "")):
                with patch.object(orquestrador, "hunter_disponivel", return_value=True):
                    with patch.object(orquestrador, "hunter_verify",
                                      return_value=fake_hunter_data):
                        v = orquestrador.validar_email("a@x.com")
    assert v.status == "verified_smtp"
    assert v.fonte_validacao == "hunter:valid"
    assert v.confianca == "alta"


def test_orquestrador_hunter_invalid_baixa_score():
    fake = {"result": "unknown", "score": 30}
    with patch.object(orquestrador, "tem_mx", return_value=(True, "mx.x.com")):
        with patch.object(orquestrador, "is_catch_all", return_value=False):
            with patch.object(orquestrador, "smtp_probe",
                              return_value=("greylisted", None, "")):
                with patch.object(orquestrador, "hunter_disponivel", return_value=True):
                    with patch.object(orquestrador, "hunter_verify", return_value=fake):
                        v = orquestrador.validar_email("a@x.com")
    assert v.status == "greylisted"
    assert v.confianca == "baixa"


def test_encontrar_email_hunter_indisponivel():
    with patch.object(orquestrador, "hunter_disponivel", return_value=False):
        out = orquestrador.encontrar_email("Joao Silva", "x.com")
    assert out is None


def test_encontrar_email_hunter_hit():
    fake = {"email": "joao.silva@x.com", "score": 88}
    with patch.object(orquestrador, "hunter_disponivel", return_value=True):
        with patch.object(orquestrador, "hunter_find", return_value=fake):
            out = orquestrador.encontrar_email("Joao Silva", "x.com")
    assert out is not None
    assert out.email == "joao.silva@x.com"
    assert out.score_hunter == 88


def test_encontrar_email_hunter_sem_hit():
    with patch.object(orquestrador, "hunter_disponivel", return_value=True):
        with patch.object(orquestrador, "hunter_find", return_value=None):
            out = orquestrador.encontrar_email("Joao Silva", "x.com")
    assert out is None
