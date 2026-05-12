import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# garantir import path
_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada1_identificacao import brasilapi, classificar


def test_validar_cnpj_dv_real():
    # VIA SUL CNPJ matriz validado em A3 tarde
    assert brasilapi.validar_cnpj_dv("32161500000100") is True
    assert brasilapi.validar_cnpj_dv("32.161.500/0001-00") is True


def test_validar_cnpj_dv_invalido():
    assert brasilapi.validar_cnpj_dv("32161500000101") is False  # DV errado
    assert brasilapi.validar_cnpj_dv("12345678901234") is False
    assert brasilapi.validar_cnpj_dv("11111111111111") is False  # todos iguais
    assert brasilapi.validar_cnpj_dv("") is False
    assert brasilapi.validar_cnpj_dv(None) is False


def test_normalizar_cnpj():
    assert brasilapi.normalizar_cnpj("32.161.500/0001-00") == "32161500000100"
    assert brasilapi.normalizar_cnpj(" abc 32 . 161 . 500 ") == "32161500"  # bom: extrai so digitos


def test_buscar_dados_cnpj_invalido_raise():
    import pytest
    with pytest.raises(ValueError):
        brasilapi.buscar_dados_cnpj("00000000000000")  # DV invalido


def test_mapear_para_dossier():
    dados_brasilapi = {
        "cnpj": "32161500000100",
        "razao_social": "CONCESSIONARIA DAS RODOVIAS INTEGRADAS DO SUL S.A.",
        "nome_fantasia": "CCR ViaSul",
        "natureza_juridica": "Sociedade Anônima Fechada",
        "cnae_fiscal": 5221400,
        "cnae_fiscal_descricao": "Concessionárias de rodovias, pontes...",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "RS",
        "municipio": "PORTO ALEGRE",
        "capital_social": 0,
        "identificador_matriz_filial": 1,
    }
    out = brasilapi.mapear_para_dossier(dados_brasilapi)
    assert out["cnpj"] == "32161500000100"
    assert out["razao_social"].startswith("CONCESSIONARIA")
    assert out["uf"] == "RS"
    assert out["matriz"] is True
    assert out["cnae_fiscal"] == "5221400"


def test_classificar_holding_por_nome():
    parcial = {"razao_social": "ITAÚSA INVESTIMENTOS S.A.", "nome_fantasia": None,
               "cnae_fiscal": "6462000", "matriz": True}
    out = classificar.classificar_organizacao(parcial)
    assert out["tipo_organizacao"] == "holding"
    assert out["confianca_classificacao"] >= 0.7


def test_classificar_filial():
    parcial = {"razao_social": "EMPRESA X S.A.", "nome_fantasia": None,
               "cnae_fiscal": "1234567", "matriz": False}
    out = classificar.classificar_organizacao(parcial)
    assert out["tipo_organizacao"] == "filial"


def test_classificar_spv_por_nome():
    parcial = {"razao_social": "ECO101 CONCESSIONARIA DE RODOVIAS S/A",
               "nome_fantasia": None, "cnae_fiscal": "5221400", "matriz": True}
    out = classificar.classificar_organizacao(parcial)
    assert out["tipo_organizacao"] == "spv_operadora"


def test_orquestrador_e2e_mockado():
    from sales_intelligence.camada1_identificacao import orquestrador
    fake_dados = {
        "cnpj": "32161500000100",
        "razao_social": "CCR VIASUL S.A.",
        "nome_fantasia": None,
        "natureza_juridica": "S.A.",
        "cnae_fiscal": 5221400,
        "cnae_fiscal_descricao": "Concessionárias de rodovias",
        "descricao_situacao_cadastral": "ATIVA",
        "uf": "RS", "municipio": "PORTO ALEGRE",
        "capital_social": 1000,
        "identificador_matriz_filial": 1,
    }
    with patch.object(brasilapi, "buscar_dados_cnpj", return_value=fake_dados):
        with patch("sales_intelligence.camada1_identificacao.orquestrador.descobrir_dominio.descobrir_dominio_oficial", return_value=None):
            with patch("sales_intelligence.db.cache_dossier.buscar", return_value=None):
                with patch("sales_intelligence.db.cache_dossier.gravar", return_value=True):
                    d = orquestrador.identificar_empresa("32161500000100")
    assert d.cnpj == "32161500000100"
    assert d.razao_social == "CCR VIASUL S.A."
    assert d.uf == "RS"
    assert d.spv_ou_matriz in ("spv_operadora", "matriz_grupo")
    assert 0.4 <= d.confianca_geral <= 1.0
