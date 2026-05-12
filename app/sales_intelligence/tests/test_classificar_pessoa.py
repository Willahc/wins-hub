"""Tests para classificar_local_part (heuristica leve, sem dict)."""
import sys
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada2_pattern_detection.classificar_pessoa import (
    classificar_local_part,
    split_concatenated,
)


# ---- pessoa: com separador ----

def test_classificar_pessoa_com_separador():
    assert classificar_local_part("thiago.lofiego") == "pessoa"
    assert classificar_local_part("bruno.carvalho") == "pessoa"
    assert classificar_local_part("pedro.terra") == "pessoa"
    assert classificar_local_part("elaine.kropf") == "pessoa"
    assert classificar_local_part("rafaella.martins") == "pessoa"
    assert classificar_local_part("joao_silva") == "pessoa"
    assert classificar_local_part("givanildo-souza") == "pessoa"


# ---- pessoa: concatenada (split heuristico V-C-V) ----

def test_classificar_pessoa_concatenada():
    assert classificar_local_part("ricardohatherly") == "pessoa"
    assert classificar_local_part("fernandabianchini") == "pessoa"


# ---- setor: lookup direto ou via separador ----

def test_classificar_setor_explicito():
    assert classificar_local_part("vale.ri") == "setor"
    assert classificar_local_part("ahe-belomonte") == "setor"
    assert classificar_local_part("contato") == "setor"
    assert classificar_local_part("acionistas") == "setor"
    assert classificar_local_part("apoio.plantao") == "setor"


# ---- setor: concatenado (exact match OU prefix-match) ----

def test_classificar_setor_concatenado():
    # exact match em SETORIAIS_EXPANDIDOS
    assert classificar_local_part("lostandfound") == "setor"
    assert classificar_local_part("atendimentocsc") == "setor"
    assert classificar_local_part("dfinri") == "setor"
    assert classificar_local_part("tarifagru") == "setor"
    assert classificar_local_part("dutymanager") == "setor"


# ---- placeholder: exact match ----

def test_classificar_placeholder():
    assert classificar_local_part("jane.doe") == "placeholder"
    assert classificar_local_part("first.last") == "placeholder"
    assert classificar_local_part("john") == "placeholder"
    assert classificar_local_part("foo") == "placeholder"


# ---- placeholder: suffix-match ----

def test_classificar_suffix_placeholder():
    # PLACEHOLDERS_SUFFIX = {'inbox', 'noreply', 'noresponse', 'donotreply'}
    assert classificar_local_part("caioinbox") == "placeholder"
    assert classificar_local_part("johnnoreply") == "placeholder"


# ---- indefinido: dados insuficientes ----

def test_classificar_indefinido():
    # len <3 sem separador
    assert classificar_local_part("ab") == "indefinido"
    assert classificar_local_part("") == "indefinido"
    # sem vogais (sem separador, split falha)
    assert classificar_local_part("rrrrrrr") == "indefinido"


# ---- divergencia aceita da spec original: random.string -> pessoa ----

def test_random_string_aceito_como_pessoa():
    """Heuristica leve sem dict nao distingue 'random' de nome.

    Defesa em profundidade: amostra unica gera confianca='baixa' que
    integracao_c3_c4 filtra como nao-usavel. Aceitavel.
    """
    assert classificar_local_part("random.string") == "pessoa"


# ---- split_concatenated unidade ----

def test_split_concatenated_unidade():
    assert split_concatenated("ricardohatherly") == ("ricardo", "hatherly")
    assert split_concatenated("fernandabianchini") == ("fernanda", "bianchini")
    assert split_concatenated("ab") is None
    assert split_concatenated("xxxxxxx") is None  # sem padrao V-C-V


# ---- nomes de empresa como standalone NUNCA sao pessoa ----

def test_nomes_empresa_classificados_como_setor():
    assert classificar_local_part("vale") == "setor"
    assert classificar_local_part("petrobras") == "setor"
    assert classificar_local_part("gru") == "setor"
    assert classificar_local_part("eletrobras") == "setor"


# ---- caso edge: prefix-match nao deve catar nome real ----

def test_prefix_match_nao_pega_valeria():
    """'valeria' contem 'vale' como prefixo mas len('vale')=4 < _PREFIX_MIN=5.

    Vai cair em split V-C-V e gerar ('vale', 'ria') -> lookup_partes pega
    'vale' como setor. Isso eh TRADE-OFF aceito (proteger vale.com domain
    > permitir Valeria de outras empresas).
    """
    # Documenta o comportamento esperado: 'valeria' classificada como setor
    # (trade-off aceito).
    assert classificar_local_part("valeria") == "setor"
