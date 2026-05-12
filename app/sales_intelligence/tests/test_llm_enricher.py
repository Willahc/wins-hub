"""Tests llm_enricher.filtrar_ex_funcionario (todos mockados, 0 cost)."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.llm_enricher.filtro_ex_funcionario import filtrar_ex_funcionario, _strip_md_fences
from sales_intelligence.llm_enricher.modelos import DecisorInput


def test_strip_md_fences_json_block():
    raw = '```json\n{"a": 1}\n```'
    assert _strip_md_fences(raw) == '{"a": 1}'


def test_strip_md_fences_plain_block():
    raw = '```\n{"b": 2}\n```'
    assert _strip_md_fences(raw) == '{"b": 2}'


def test_strip_md_fences_no_fences():
    raw = '{"c": 3}'
    assert _strip_md_fences(raw) == '{"c": 3}'


def _mock_msg(text: str, in_tok: int = 200, out_tok: int = 80):
    """Cria mock de mensagem Anthropic com .content[0].text + .usage."""
    m = MagicMock()
    m.content = [MagicMock(text=text)]
    m.usage = MagicMock(input_tokens=in_tok, output_tokens=out_tok)
    return m


def _decisor_paulo() -> DecisorInput:
    return DecisorInput(
        nome_pessoa="Paulo Tanure",
        cargo_raw="ex-Diretor de Suprimentos",
        snippet_origem="Senior Procurement Manager. Grupo EcoRodovias. nov. de 2016 - set. de 2017 11 meses.",
        url_origem="https://br.linkedin.com/in/paulo-tanure/pt",
    )


def _decisor_carlos() -> DecisorInput:
    return DecisorInput(
        nome_pessoa="Carlos Lima",
        cargo_raw="Gerente de Suprimentos",
        snippet_origem="Carlos Lima - Gerente de Suprimentos | Strategic Sourcing | LinkedIn",
        url_origem="https://br.linkedin.com/in/carlos-lima-ba0b3569",
    )


@patch("sales_intelligence.llm_enricher.filtro_ex_funcionario.get_client")
def test_filtro_paulo_tanure_ex_funcionario(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        '{"trabalha_atualmente": false, "confianca": "alta", '
        '"empresa_atual_inferida": "ABC Construtora", '
        '"empresa_anterior_inferida": "Ecorodovias", '
        '"razao": "ex-Diretor 2016-2017 declarado no snippet"}',
        in_tok=250, out_tok=90,
    )
    mock_get_client.return_value = mock_client

    r = filtrar_ex_funcionario(_decisor_paulo(), "Ecorodovias")
    assert r.trabalha_atualmente is False
    assert r.confianca == "alta"
    assert r.empresa_anterior_inferida == "Ecorodovias"
    assert r.empresa_atual_inferida == "ABC Construtora"
    assert "ex-Diretor" in r.razao
    # custo: 250e-6 + 90*5e-6 = 0.00025 + 0.00045 = 0.0007
    assert abs(r.custo_usd - 0.0007) < 1e-9
    assert r.tokens_input == 250
    assert r.tokens_output == 90


@patch("sales_intelligence.llm_enricher.filtro_ex_funcionario.get_client")
def test_filtro_carlos_lima_atual(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        '{"trabalha_atualmente": true, "confianca": "alta", '
        '"empresa_atual_inferida": "Ecorodovias", '
        '"empresa_anterior_inferida": null, '
        '"razao": "sem qualificador temporal historico"}',
        in_tok=180, out_tok=60,
    )
    mock_get_client.return_value = mock_client

    r = filtrar_ex_funcionario(_decisor_carlos(), "Ecorodovias")
    assert r.trabalha_atualmente is True
    assert r.confianca == "alta"
    assert r.empresa_atual_inferida == "Ecorodovias"
    assert r.empresa_anterior_inferida is None
    assert r.tokens_input == 180
    # custo: 180e-6 + 60*5e-6 = 0.00018 + 0.0003 = 0.00048
    assert abs(r.custo_usd - 0.00048) < 1e-9


@patch("sales_intelligence.llm_enricher.filtro_ex_funcionario.get_client")
def test_filtro_fence_block_strippado(mock_get_client):
    """Haiku as vezes envolve em ```json...```. Parser deve stripar."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        '```json\n{"trabalha_atualmente": false, "confianca": "alta", '
        '"empresa_atual_inferida": null, "empresa_anterior_inferida": "Petrobras", '
        '"razao": "Datas 2016-2017 explicitas"}\n```',
        in_tok=300, out_tok=120,
    )
    mock_get_client.return_value = mock_client

    r = filtrar_ex_funcionario(_decisor_paulo(), "Petrobras")
    assert r.trabalha_atualmente is False
    assert r.confianca == "alta"
    assert "Datas" in r.razao
    # NAO eh fallback baixa
    assert r.razao != "parse JSON falhou: ..."


@patch("sales_intelligence.llm_enricher.filtro_ex_funcionario.get_client")
def test_filtro_json_invalido_fallback(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        "Aqui vai um texto que NAO eh JSON.",
        in_tok=200, out_tok=50,
    )
    mock_get_client.return_value = mock_client

    r = filtrar_ex_funcionario(_decisor_carlos(), "Ecorodovias")
    # conservador
    assert r.trabalha_atualmente is True
    assert r.confianca == "baixa"
    assert "parse JSON falhou" in r.razao
    # custo ainda eh capturado
    assert r.tokens_input == 200
    assert r.tokens_output == 50
    assert r.custo_usd > 0


@patch("sales_intelligence.llm_enricher.filtro_ex_funcionario.get_client")
def test_filtro_api_exception_fallback(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("rate limit")
    mock_get_client.return_value = mock_client

    r = filtrar_ex_funcionario(_decisor_carlos(), "Ecorodovias")
    assert r.trabalha_atualmente is True
    assert r.confianca == "baixa"
    assert "erro API" in r.razao
    assert "rate limit" in r.razao
    # sem chamada bem-sucedida -> sem custo capturado
    assert r.custo_usd == 0.0
    assert r.tokens_input == 0
    assert r.tokens_output == 0
