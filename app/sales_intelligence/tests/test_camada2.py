import sys
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada2_pattern_detection import detectar_padrao, gerar_email


def test_detectar_padrao_dot_separator():
    emails = [
        "joao.silva@empresa.com",
        "maria.santos@empresa.com",
        "carlos.lima@empresa.com",
        "ana.oliveira@empresa.com",
    ]
    pat = detectar_padrao.detectar_padrao(emails)
    assert pat is not None
    assert pat.padrao == "{first}.{last}"
    assert pat.confianca == "alta"
    assert pat.dominio_origem == "empresa.com"
    assert pat.amostra_total == 4


def test_detectar_padrao_underscore():
    emails = [
        "joao_silva@empresa.com",
        "maria_santos@empresa.com",
        "carlos_lima@empresa.com",
    ]
    pat = detectar_padrao.detectar_padrao(emails)
    assert pat is not None
    assert pat.padrao == "{first}_{last}"
    assert pat.confianca == "alta"


def test_detectar_padrao_inconclusivo():
    # so 2 emails, formatos diferentes
    emails = [
        "contato@empresa.com",  # filtrado (generico)
        "qwerty12345@empresa.com",  # formato estranho
    ]
    pat = detectar_padrao.detectar_padrao(emails)
    assert pat is None


def test_detectar_padrao_vazio():
    assert detectar_padrao.detectar_padrao([]) is None


def test_gerar_email_aplica_padrao():
    from sales_intelligence.models.email_pattern import EmailPattern
    pat = EmailPattern(
        padrao="{first}.{last}",
        confianca="alta",
        exemplos=["joao.silva@empresa.com"],
        amostra_total=4,
        dominio_origem="empresa.com",
    )
    email = gerar_email.gerar_email("Marcello Guidotti", pat)
    assert email == "marcello.guidotti@empresa.com"


def test_gerar_email_normaliza_acentos():
    from sales_intelligence.models.email_pattern import EmailPattern
    pat = EmailPattern(padrao="{first}.{last}", confianca="alta",
                       exemplos=["a.b@x.com"], amostra_total=3, dominio_origem="x.com")
    email = gerar_email.gerar_email("João da Silva Pereira", pat)
    assert email == "joao.pereira@x.com"  # primeiro + ultimo, sem acento


def test_gerar_email_padrao_inicial():
    from sales_intelligence.models.email_pattern import EmailPattern
    pat = EmailPattern(padrao="{f}{last}", confianca="alta",
                       exemplos=["jsilva@x.com"], amostra_total=3, dominio_origem="x.com")
    email = gerar_email.gerar_email("Joao Silva", pat)
    assert email == "jsilva@x.com"


def test_gerar_email_recusa_baixa_confianca():
    from sales_intelligence.models.email_pattern import EmailPattern
    pat = EmailPattern(padrao="{first}.{last}", confianca="baixa",
                       exemplos=["x.y@z.com"], amostra_total=1, dominio_origem="z.com")
    email = gerar_email.gerar_email("Joao Silva", pat)
    assert email is None  # anti-alucinacao


def test_gerar_email_padrao_none():
    email = gerar_email.gerar_email("Joao Silva", None, "x.com")
    assert email is None
