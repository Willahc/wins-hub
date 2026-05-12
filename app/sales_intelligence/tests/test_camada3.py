import sys
from pathlib import Path
from datetime import datetime, date, timedelta

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada3_decisores import mapping
from sales_intelligence.camada3_decisores.linkedin_search import (
    _proximity_check, TEMPORAL_HISTORICO,
)
from sales_intelligence.camada3_decisores.orquestrador import _consolidar, _chave_pessoa
from sales_intelligence.models.decisor import DecisorBruto, Decisor


def test_normalizar_cargo_ptbr():
    assert mapping.normalizar_cargo("Gerente de Engenharia") == "GERENTE_ENGENHARIA"
    assert mapping.normalizar_cargo("gerente de engenharia") == "GERENTE_ENGENHARIA"
    assert mapping.normalizar_cargo("GERENTE DE ENGENHARIA") == "GERENTE_ENGENHARIA"


def test_normalizar_cargo_en():
    assert mapping.normalizar_cargo("Engineering Manager") == "GERENTE_ENGENHARIA"
    assert mapping.normalizar_cargo("Procurement Director") == "GERENTE_SUPRIMENTOS"
    assert mapping.normalizar_cargo("Plant Manager") == "GERENTE_INDUSTRIAL"


def test_normalizar_cargo_substring_match():
    # cargo extenso com sufixo extra ainda casa via substring
    assert mapping.normalizar_cargo("Engineering Manager - Industrial Plant") == "GERENTE_ENGENHARIA"


def test_normalizar_cargo_inexistente_retorna_none():
    assert mapping.normalizar_cargo("Cargo Esquisito") is None
    assert mapping.normalizar_cargo("") is None
    assert mapping.normalizar_cargo(None) is None


def test_detectar_idioma_ptbr():
    assert mapping.detectar_idioma("Diretor de Compras") == "pt-br"
    assert mapping.detectar_idioma("Engenheiro Senior") == "pt-br"


def test_detectar_idioma_en():
    assert mapping.detectar_idioma("Procurement Director") == "en"
    assert mapping.detectar_idioma("Senior Engineer") == "en"


def test_determinar_nivel():
    assert mapping.determinar_nivel("GERENTE_ENGENHARIA") == "tatico"
    assert mapping.determinar_nivel("ENGENHEIRO_MECANICO_CIVIL") == "operacional"
    assert mapping.determinar_nivel("OUTRO") == "estrategico"
    assert mapping.determinar_nivel(None) is None


def test_proximity_check_passa():
    snippet = "Joao Silva - Engineering Manager at Petrobras desde 2020"
    assert _proximity_check(snippet, "Engineering Manager", "Petrobras") is True


def test_proximity_check_falha():
    # cargo no inicio, empresa muito longe (>200 chars)
    snippet = "Joao Silva - Engineering Manager. " + ("x " * 200) + "Petrobras"
    assert _proximity_check(snippet, "Engineering Manager", "Petrobras") is False


def test_temporal_historico_pega_ex():
    assert TEMPORAL_HISTORICO.search("ex-Diretor da empresa") is not None
    assert TEMPORAL_HISTORICO.search("former CEO") is not None
    assert TEMPORAL_HISTORICO.search("trabalhou como gerente") is not None


def test_temporal_historico_nao_pega_atual():
    assert TEMPORAL_HISTORICO.search("Diretor da empresa desde 2020") is None
    assert TEMPORAL_HISTORICO.search("Engineering Manager at Petrobras") is None


def test_consolidar_dedup_eleva_confianca():
    """Mesma pessoa em DDG (media) + CVM (alta) deve consolidar com fonte_secundaria."""
    brutos = [
        DecisorBruto(nome_pessoa="Maria Silva", cargo_raw="Diretora Financeira",
                     tipo_cargo="OUTRO", cargo_nivel="estrategico",
                     fonte_descoberta="ddg", confianca="media",
                     snippet_origem="...", url_origem="..."),
        DecisorBruto(nome_pessoa="MARIA SILVA", cargo_raw="CFO",
                     tipo_cargo="OUTRO", cargo_nivel="estrategico",
                     fonte_descoberta="cvm", confianca="alta",
                     snippet_origem="CVM IPE", url_origem="..."),
    ]
    out = _consolidar(brutos, "12345678000195")
    assert len(out) == 1
    # CVM rank > DDG, entao escolhe CVM como primaria
    assert out[0].fonte_descoberta == "cvm"
    assert out[0].fonte_secundaria == "ddg"
    assert out[0].confianca == "alta"


def test_consolidar_chave_normalizada():
    """Joao Silva, JOAO SILVA, Joao da Silva sao a mesma pessoa apos chave."""
    assert _chave_pessoa("Joao Silva") == _chave_pessoa("JOAO SILVA")
    assert _chave_pessoa("João Silva") == _chave_pessoa("Joao Silva")


def test_score_relevancia_alta_completo():
    brutos = [
        DecisorBruto(nome_pessoa="Carlos Lima", cargo_raw="Engineering Manager",
                     cargo_normalizado="Engineering Manager", tipo_cargo="GERENTE_ENGENHARIA",
                     cargo_nivel="tatico", linkedin_slug="carlos-lima",
                     fonte_descoberta="ddg", confianca="alta",
                     snippet_origem="...", url_origem="..."),
    ]
    out = _consolidar(brutos, "12345678000195")
    # base tatico=0.7 + alta=0.2 + tipo_cargo=0.1 + linkedin=0.1 = 1.1 -> cap 1.0
    assert out[0].score_relevancia == 1.0


def test_score_relevancia_baixa_estrategico():
    brutos = [
        DecisorBruto(nome_pessoa="X Y", cargo_raw="?", tipo_cargo="OUTRO",
                     cargo_nivel="estrategico", fonte_descoberta="ddg",
                     confianca="baixa", snippet_origem="...", url_origem="..."),
    ]
    out = _consolidar(brutos, "12345678000195")
    # estrategico=0.3, sem bonus
    assert out[0].score_relevancia == 0.3


def test_pydantic_decisor_valido():
    d = Decisor(
        cnpj="12345678000195", nome_pessoa="Test", cargo_raw="Manager",
        tipo_cargo="GERENTE_PROJETOS", confianca="media",
        fonte_descoberta="ddg", snippet_origem="...", url_origem="...",
        score_relevancia=0.7,
    )
    assert d.tipo_cargo == "GERENTE_PROJETOS"
    assert 0.0 <= d.score_relevancia <= 1.0


def test_buckets_completos():
    """48 termos -> divididos em buckets de 10."""
    total = sum(len(b) for b in mapping.QUERY_BUCKETS)
    assert total == len(mapping.TERMOS_BUSCA)
    assert len(mapping.TERMOS_BUSCA) >= 48
