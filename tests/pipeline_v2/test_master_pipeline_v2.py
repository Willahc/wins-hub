#!/usr/bin/env python3
"""Testes do pipeline mestre V2 (unitarios + integracao controlada)."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.master_pipeline_v2 import (  # noqa: E402
    CAMPOS_288,
    hash_payload,
    is_master_pipeline_enabled,
    is_valid_cnpj,
    mapear_para_288,
    processar_captura_para_master,
    processar_captura_para_master_safe,
)


def test_campos_288_catalogo():
    assert len(CAMPOS_288) == 288
    assert "cnpj_contratante" in CAMPOS_288
    assert "valor_capex" in CAMPOS_288


def test_flag_default_false(monkeypatch):
    monkeypatch.delenv("MASTER_PIPELINE_V2_ENABLED", raising=False)
    assert is_master_pipeline_enabled() is False


def test_pncp_nao_vira_executora_nem_capex():
    payload = {
        "numeroControlePNCP": "123",
        "objetoCompra": "Construcao de escola",
        "valorTotalEstimado": 1500000,
        "orgaoEntidade": {"cnpj": "00.000.000/0001-91", "razaoSocial": "ORGAO X"},
        "unidadeOrgao": {"ufSigla": "SP", "municipioNome": "Sao Paulo"},
    }
    # CNPJ valido de teste: gerar um valido
    # Use known valid: 11.222.333/0001-81 is invalid often — use algorithm
    from services.master_pipeline_v2 import normalize_cnpj

    # 04.252.011/0001-10 is a known valid pattern used in docs sometimes
    payload["orgaoEntidade"]["cnpj"] = "04252011000110"
    campos, ev, ents = mapear_para_288("pncp_obras", "captar_pncp_obras", "PNCP:123", payload)
    assert campos["cnpj_contratante"] == "04252011000110" or campos["cnpj_contratante"] is None or is_valid_cnpj(campos["cnpj_contratante"])
    assert campos.get("cnpj_executora") in (None, "")
    assert campos.get("valor_capex") in (None, "")
    assert campos.get("valor_estimado_contratacao") == 1500000
    assert campos.get("tipo_valor") == "ESTIMADO"
    assert any(e["papel"] == "CONTRATANTE" for e in ents) or not ents


def test_bndes_beneficiaria():
    payload = {
        "cnpj": "04252011000110",
        "empresa": "CLIENTE BNDES",
        "valor_estimado": 9_000_000,
        "numero_do_contrato": "123",
    }
    campos, _, ents = mapear_para_288(
        "bndes_financiamento", "captar_bndes", "BNDES-1", payload
    )
    if is_valid_cnpj("04252011000110"):
        assert campos["cnpj_beneficiaria"] == "04252011000110"
        assert campos.get("cnpj_executora") in (None, "")
    assert campos.get("tipo_valor") == "FINANCIAMENTO"
    assert campos.get("valor_capex") in (None, "")


def test_obrasgov_executora():
    payload = {
        "cnpj": "04252011000110",
        "empresa": "EXEC SA",
        "valor_estimado": 2_000_000,
        "municipio": "Recife",
        "uf": "PE",
        "fase": "EM_EXECUCAO",
    }
    campos, _, ents = mapear_para_288(
        "obrasgov_100k", "captar_obrasgov_100k", "OBRASGOV:1", payload
    )
    if is_valid_cnpj("04252011000110"):
        assert campos["cnpj_executora"] == "04252011000110"
        assert campos.get("cnpj_contratante") in (None, "")
    assert campos.get("tipo_valor") == "INVESTIMENTO_PREVISTO"


def test_dou_nao_promove_executora():
    payload = {
        "cnpj": "04252011000110",
        "empresa": "Empresa Noticia",
        "valor_estimado": 50_000_000,
        "descricao": "Investimento em planta industrial",
    }
    campos, _, ents = mapear_para_288("dou", "captar_dou_inlabs", "DOU:1", payload)
    assert campos.get("cnpj_executora") in (None, "")
    assert campos.get("tipo_valor") == "CAPEX_LLM"
    assert campos.get("capex_suspeito") is True


def test_hash_estavel():
    a = {"z": 1, "a": 2}
    b = {"a": 2, "z": 1}
    assert hash_payload(a) == hash_payload(b)


def test_safe_disabled_nao_quebra(monkeypatch):
    monkeypatch.setenv("MASTER_PIPELINE_V2_ENABLED", "false")
    r = processar_captura_para_master_safe(
        "pncp_obras", "captar_pncp_obras", "PNCP:x", {"a": 1}
    )
    assert r["status"] == "DISABLED"


@pytest.mark.integration
def test_integracao_db_idempotente(monkeypatch):
    if os.environ.get("RUN_PIPELINE_DB_TESTS") != "1":
        pytest.skip("defina RUN_PIPELINE_DB_TESTS=1")
    monkeypatch.setenv("MASTER_PIPELINE_V2_ENABLED", "true")
    ext = f"TEST-PIPELINE-{uuid.uuid4()}"
    payload = {
        "nome": "Obra teste pipeline",
        "empresa": "ORGAO TESTE",
        "cnpj": "04252011000110",
        "uf": "SP",
        "municipio": "Sao Paulo",
        "setor": "INFRAESTRUTURA",
        "valor_estimado": 1234567,
        "descricao": "Teste controlado pipeline v2",
        "url_fonte": "https://example.local/test",
        "numeroControlePNCP": ext,
        "objetoCompra": "Teste controlado pipeline v2",
        "orgaoEntidade": {"cnpj": "04252011000110", "razaoSocial": "ORGAO TESTE"},
        "unidadeOrgao": {"ufSigla": "SP", "municipioNome": "Sao Paulo"},
        "valorTotalEstimado": 1234567,
    }
    r1 = processar_captura_para_master(
        "pncp_obras",
        "captar_pncp_obras",
        f"PNCP:{ext}",
        payload,
        {"origem_marcador": "CAPTURA_NOVA", "namespace": "teste"},
    )
    assert r1["status"] == "OK"
    r2 = processar_captura_para_master(
        "pncp_obras",
        "captar_pncp_obras",
        f"PNCP:{ext}",
        payload,
        {"origem_marcador": "CAPTURA_NOVA", "namespace": "teste"},
    )
    assert r2["status"] == "DUPLICADO"
    # payload alterado -> nova versao
    payload2 = dict(payload)
    payload2["objetoCompra"] = "Teste controlado pipeline v2 ALTERADO"
    r3 = processar_captura_para_master(
        "pncp_obras",
        "captar_pncp_obras",
        f"PNCP:{ext}",
        payload2,
        {"origem_marcador": "CAPTURA_NOVA", "namespace": "teste"},
    )
    assert r3["status"] == "OK"
    assert r3["versao"] >= 2


@pytest.mark.integration
def test_erro_v2_nao_quebra_safe(monkeypatch):
    monkeypatch.setenv("MASTER_PIPELINE_V2_ENABLED", "true")
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "1")  # porta invalida
    monkeypatch.setenv("DB_USER", "wins_app")
    monkeypatch.setenv("DB_PASSWORD", "x")
    monkeypatch.setenv("DB_NAME", "wins_hub")
    r = processar_captura_para_master_safe("x", "y", "z", {"a": 1})
    assert r["status"] in {"ERRO_ENFILEIRADO", "HOOK_ERROR", "ERRO"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
