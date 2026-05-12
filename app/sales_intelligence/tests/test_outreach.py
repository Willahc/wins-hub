"""Tests outreach.gerar_outreach (mockados, 0 cost API)."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.outreach.gerador import gerar_outreach, _strip_md_fences
from sales_intelligence.outreach.modelos import OutreachInput


def _input_basico() -> OutreachInput:
    return OutreachInput(
        nome_pessoa="João Silva",
        cargo_raw="Procurement Manager",
        empresa_nome="Empresa Teste S.A.",
        email="joao.silva@empresa.example.com",
        snippet_origem="Procurement Manager at Empresa Teste since 2020",
        obra_nome="Projeto Alfa",
        obra_valor_formatado="R$ 384 bi",
        obra_fase="em execucao",
        obra_descricao="Descrição genérica de obra",
        obra_uf="RJ",
        fornecedor_nome="Fornecedor Teste",
        fornecedor_servicos=["Geotecnia", "Sondagens"],
        fornecedor_referencias=["Projeto Beta R$19bi", "Projeto Gama"],
    )


def _mock_msg(text: str, in_tok: int = 800, out_tok: int = 300):
    m = MagicMock()
    m.content = [MagicMock(text=text)]
    m.usage = MagicMock(input_tokens=in_tok, output_tokens=out_tok)
    return m


@patch("sales_intelligence.outreach.gerador.get_client")
def test_gerar_outreach_sucesso_basico(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        '{"assunto": "Alfa e geotecnia", '
        '"corpo": "João, com Projeto Alfa em execução, o fornecedor é relevante. Vamos conversar?", '
        '"cta": "Conversamos sobre Alfa?", '
        '"raciocinio": "Ângulo geotécnico em Alfa"}',
        in_tok=820, out_tok=310,
    )
    mock_get_client.return_value = mock_client

    r = gerar_outreach(_input_basico())
    assert r.assunto == "Alfa e geotecnia"
    assert "João" in r.corpo
    assert "Alfa" in r.cta
    assert r.tokens_input == 820
    assert r.tokens_output == 310
    # Sonnet pricing: 820*3e-6 + 310*15e-6 = 0.00246 + 0.00465 = 0.00711
    assert abs(r.custo_usd - 0.00711) < 1e-6


@patch("sales_intelligence.outreach.gerador.get_client")
def test_gerar_outreach_json_invalido_fallback(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        "isso nao eh JSON valido",
        in_tok=800, out_tok=20,
    )
    mock_get_client.return_value = mock_client

    r = gerar_outreach(_input_basico())
    # fallback conservador
    assert "Projeto Alfa" in r.assunto  # template fallback usa obra_nome
    assert r.cta == "Podemos conversar brevemente?"
    assert "FALLBACK" in r.raciocinio
    assert "parse JSON" in r.raciocinio
    # custo ainda capturado (API retornou)
    assert r.custo_usd > 0
    assert r.tokens_input == 800


@patch("sales_intelligence.outreach.gerador.get_client")
def test_gerar_outreach_api_exception_fallback(mock_get_client):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("rate limit ou network")
    mock_get_client.return_value = mock_client

    r = gerar_outreach(_input_basico())
    assert "Projeto Alfa" in r.assunto
    assert "FALLBACK" in r.raciocinio
    assert "API erro" in r.raciocinio
    assert "rate limit" in r.raciocinio
    # sem API call bem-sucedida -> sem custo
    assert r.custo_usd == 0.0
    assert r.tokens_input == 0


def test_strip_md_fences_outreach():
    """Regression test: Sonnet as vezes envolve em ```json ... ```."""
    raw = '```json\n{"assunto": "X"}\n```'
    assert _strip_md_fences(raw) == '{"assunto": "X"}'

    raw_plain = '```\n{"a": 1}\n```'
    assert _strip_md_fences(raw_plain) == '{"a": 1}'

    raw_no_fence = '{"clean": true}'
    assert _strip_md_fences(raw_no_fence) == '{"clean": true}'


@patch("sales_intelligence.outreach.gerador.get_client")
def test_gerar_outreach_strip_fences_real(mock_get_client):
    """Cenario real: Sonnet envolve resposta em fence + parser deve stripar."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_msg(
        '```json\n{"assunto": "Alfa", "corpo": "Texto", '
        '"cta": "Conversamos?", "raciocinio": "Estrategia X"}\n```',
        in_tok=850, out_tok=350,
    )
    mock_get_client.return_value = mock_client

    r = gerar_outreach(_input_basico())
    assert r.assunto == "Alfa"
    assert "FALLBACK" not in r.raciocinio
    assert r.tokens_output == 350
