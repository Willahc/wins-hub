"""Tests temporal_gate.detectar_ex_funcionario."""
import sys
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada3_decisores.temporal_gate import detectar_ex_funcionario


def test_detecta_range_anos_explicito():
    r = detectar_ex_funcionario("Paulo Tanure, ex-Diretor 2016-2017")
    assert r.eh_ex is True
    # ordem do iter: ex_prefix_pt vem antes de range_anos_explicito? Sim, range eh primeiro.
    assert r.padrao_match == "range_anos_explicito"
    assert r.confianca == "alta"


def test_detecta_ex_prefix():
    r = detectar_ex_funcionario("ex-Diretor de Suprimentos Grupo X")
    assert r.eh_ex is True
    assert r.confianca == "alta"
    assert r.padrao_match == "ex_prefix_pt"


def test_detecta_former():
    r = detectar_ex_funcionario("former CEO at Acme Corp")
    # "CEO" nao esta na lista, mas o regex eh former + cargo. CEO=president? nao, CEO sozinho.
    # Lista atual: director|manager|coordinator|chief|president|engineer|analyst|head|vp
    # CEO seria "chief executive officer" mas snippet diz "CEO" abrev.
    # Vamos testar com "former Director" pra garantir match.
    r2 = detectar_ex_funcionario("former Director at Acme Corp")
    assert r2.eh_ex is True
    assert r2.padrao_match == "former_prefix_en"


def test_detecta_saiu_em():
    r = detectar_ex_funcionario("Rogerio saiu em 2025 da empresa")
    assert r.eh_ex is True
    # saiu_em_ano vem antes de aposentado, mas range nao bate
    assert r.padrao_match == "saiu_em_ano"


def test_detecta_range_meses():
    # padrao "mai - out 2025" (com espaco)
    r = detectar_ex_funcionario("mai - out 2025 trabalhando na Eco")
    assert r.eh_ex is True
    assert r.padrao_match == "range_meses_ano"


def test_atual_sem_marcadores():
    r = detectar_ex_funcionario("Carlos Lima, Gerente de Suprimentos Ecorodovias")
    assert r.eh_ex is False


def test_atual_com_data_irrelevante():
    # "1995" sozinho sem range nao bate "X-Y" pattern
    r = detectar_ex_funcionario("Empresa fundada em 1995, gerente atual de operacoes")
    assert r.eh_ex is False


def test_snippet_vazio():
    r = detectar_ex_funcionario("")
    assert r.eh_ex is False


def test_conservador_dubio():
    # Sem regex match = atual (conservador, na duvida nao exclui)
    r = detectar_ex_funcionario("Trabalhou na empresa por algum tempo")
    # "trabalhou" nao esta nos patterns; nem range; nem ex-prefix
    assert r.eh_ex is False


def test_aposentado():
    r = detectar_ex_funcionario("Joao Silva, aposentado em 2024")
    assert r.eh_ex is True
    # range nao bate (2024 sozinho), aposentado eh primeiro a casar das regras restantes
    # mas saiu_em_ano vem antes... saiu? nao tem "saiu". Entao aposentado.
    assert r.padrao_match == "aposentado"


def test_ate_ano():
    r = detectar_ex_funcionario("Director ate 2023 na empresa X")
    assert r.eh_ex is True
    assert r.padrao_match == "ate_ano"
