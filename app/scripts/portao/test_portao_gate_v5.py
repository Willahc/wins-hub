#!/usr/bin/env python3
"""Testes unitários do Portão v5 (sem DB)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from portao_gate_v5 import (  # noqa: E402
    decidir_portao,
    classificar_comercial_pos_enriquecimento,
    VALOR_MINIMO,
)


def test_construcao_clara_aprovada():
    d = decidir_portao(
        {
            "nome": "Construção de unidade industrial de alimentos",
            "descricao": "Implantação de fábrica e galpão industrial em MT",
            "setor": "INDUSTRIAL",
            "fonte": "pncp_full",
            "valor_estimado": 5_000_000,
            "url_fonte": "https://exemplo.gov.br/obra/1",
        }
    )
    assert d.status_portao == "APROVADA", d


def test_compra_material_rejeitada():
    d = decidir_portao(
        {
            "nome": "Aquisição de materiais de construção",
            "descricao": "Compra de material elétrico sem instalação",
            "setor": "INFRAESTRUTURA",
            "fonte": "pncp_full",
            "valor_estimado": 500_000,
        }
    )
    assert d.status_portao == "REJEITADA", d


def test_software_rejeitado():
    d = decidir_portao(
        {
            "nome": "Licença de software de gestão",
            "descricao": "Aquisição de SaaS ERP",
            "setor": "INDUSTRIAL",
            "fonte": "pncp_full",
            "valor_estimado": 800_000,
        }
    )
    assert d.status_portao == "REJEITADA", d


def test_manutencao_rotina_rejeitada():
    d = decidir_portao(
        {
            "nome": "Serviços de manutenção predial",
            "descricao": "Manutenção preventiva e corretiva de rotina",
            "setor": "INFRAESTRUTURA",
            "fonte": "pncp_full",
            "valor_estimado": 300_000,
        }
    )
    assert d.status_portao == "REJEITADA", d


def test_manutencao_estrutural_aprovada():
    d = decidir_portao(
        {
            "nome": "Recuperação estrutural de ponte",
            "descricao": "Manutenção estrutural e reforço estrutural da ponte rodoviária",
            "setor": "INFRAESTRUTURA",
            "fonte": "obrasgov_100k",
            "valor_estimado": 12_000_000,
            "url_fonte": "https://exemplo.gov.br/p",
        }
    )
    assert d.status_portao == "APROVADA", d


def test_engenharia_generica_em_analise():
    d = decidir_portao(
        {
            "nome": "Serviços de engenharia",
            "descricao": "Prestação de serviços de engenharia diversos",
            "setor": "INFRAESTRUTURA",
            "fonte": "pncp_full",
            "valor_estimado": 400_000,
        }
    )
    assert d.status_portao == "EM_ANALISE", d


def test_abaixo_100k_rejeitada():
    d = decidir_portao(
        {
            "nome": "Construção de pequeno depósito",
            "descricao": "Construção de galpão auxiliar",
            "setor": "INDUSTRIAL",
            "fonte": "pncp_full",
            "valor_estimado": 50_000,
        }
    )
    assert d.status_portao == "REJEITADA", d
    assert VALOR_MINIMO == 100_000


def test_valor_ausente_em_analise():
    d = decidir_portao(
        {
            "nome": "Construção de hospital municipal",
            "descricao": "Implantação de hospital e edifício de saúde",
            "setor": "INFRAESTRUTURA",
            "fonte": "obrasgov_100k",
            "valor_estimado": None,
            "url_fonte": "https://x",
        }
    )
    assert d.status_portao == "EM_ANALISE", d


def test_noticia_vaga_em_analise():
    d = decidir_portao(
        {
            "nome": "Empresa anuncia investimento",
            "descricao": "Grupo confirma aporte no estado",
            "setor": "INDUSTRIAL",
            "fonte": "noticia_moneytimes",
            "fonte_tipo": "NOTICIA",
            "valor_estimado": 1_400_000_000,
        }
    )
    assert d.status_portao == "EM_ANALISE", d


def test_licitacao_construcao_aprovada():
    d = decidir_portao(
        {
            "nome": "Licitação para construção de escola",
            "descricao": "Construção de edifício escolar e infraestrutura",
            "setor": "INFRAESTRUTURA",
            "fonte": "pncp_civil_100k",
            "fase": "LICITACAO_ABERTA",
            "valor_estimado": 2_500_000,
            "url_fonte": "https://pncp.gov.br/x",
        }
    )
    assert d.status_portao == "APROVADA", d


def test_classificacao_pos_enrich():
    obra = {"status_portao": "APROVADA", "empresa": "ACME SA"}
    assert classificar_comercial_pos_enriquecimento(obra, {}) == "BRONZE"
    assert (
        classificar_comercial_pos_enriquecimento(
            obra, {"decisor": {"nome": "João", "email": "a@b.com"}, "email_validado": True}
        )
        == "OURO"
    )
    assert (
        classificar_comercial_pos_enriquecimento(
            obra, {"decisor": {"nome": "João"}}
        )
        == "PRATA"
    )
    assert (
        classificar_comercial_pos_enriquecimento(
            {"status_portao": "APROVADA"}, {}
        )
        == "PIPELINE"
    )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
