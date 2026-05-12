"""Classificador de local-parts: pessoa | setor | placeholder | indefinido.

Heuristica leve, sem dicionarios de nomes (que ficam datados e quebram
em sobrenomes estrangeiros: Hatherly, Bianchini, Lofiego, Kropf...).
Anti-alucinacao: defesa em profundidade — amostra fraca devolve
confianca 'baixa' que integracao_c3_c4 ja filtra como nao-usavel.

Funcoes publicas:
  classificar_local_part(local) -> str
  split_concatenated(local) -> tuple | None
"""
import re
from typing import Optional, Tuple

VOGAIS = set("aeiouy")

# Placeholders de docs/forms/tutoriais — NUNCA pessoa real
PLACEHOLDERS_LOCAL = {
    "jane", "doe", "john", "jane.doe", "john.doe",
    "first", "last", "firstname", "lastname",
    "first.last", "firstname.lastname",
    "example", "exemplo", "teste", "test",
    "usuario", "user", "username",
    "nome", "sobrenome",
    "fulano", "beltrano", "sicrano",
    "foo", "bar", "baz",
    "mail", "email",
    "seu", "seunome", "seu_nome",
}

# Sufixos que indicam alias automatico/template (so SUFFIX, prefix gera FPs)
PLACEHOLDERS_SUFFIX = {"inbox", "noreply", "noresponse", "donotreply"}

# Setoriais — padrao geral + BR
GENERICOS_LOCAL = {
    "contato", "contact", "info", "admin", "noreply", "no-reply", "no_reply",
    "noresponse", "do-not-reply", "sac", "atendimento", "suporte", "support",
    "help", "imprensa", "comunicacao", "rh",
    "vendas", "comercial", "marketing", "financeiro", "tesouraria",
    "postmaster", "abuse", "mailer-daemon", "webmaster", "email",
    "selecao", "recrutamento", "vagas", "trabalheconosco",
    "ouvidoria", "reclamacao", "reclamacoes",
    "tarifa", "tarifas", "pedagio",
    "cac", "canalde", "canaldefornecedores", "fornecedores",
    "faleconosco", "duvida", "duvidas",
    "apoio", "plantao", "urgencia", "emergencia",
    "cobranca", "faturamento", "pagamento",
    "compras", "licitacao", "licitacoes", "compliance",
}

# Setoriais descobertos no smoke 7 empresas (2026-05-11)
SETORIAIS_EXPANDIDOS = {
    # RI / governanca
    "ri", "ir", "investor", "investidor", "privacy", "privacidade",
    "acionistas", "bunker",
    # Eletrobras / industria
    "ahe", "belomonte", "csc", "atendimentocsc", "inbox", "cinbox", "dfinri",
    # Ecorodovias / concessoes
    "comite", "comitedeetica", "grupocompliance",
    # GRU / aeroporto
    "lostandfound", "perdidoseachados", "dutymanager",
    "tarifagru", "cargas", "contencioso",
    "credenciamento", "incentivos", "treinamentos",
    # Petrobras
    "cc-rfisc",
    # Genericos adicionais
    "invest",
    # Nomes de empresa como local-part = NUNCA pessoa
    "vale", "petrobras", "eletrobras", "ecorodovias", "shell", "mrn", "gru",
}

# Lookup unificado para 'setor'
_SETORIAIS_TOTAL = GENERICOS_LOCAL | SETORIAIS_EXPANDIDOS

# Prefix-match so para termos >=5 chars (evita 'vale' colidir com 'valeria')
_PREFIX_MIN = 5
_SETORIAIS_PREFIX = {s for s in _SETORIAIS_TOTAL if len(s) >= _PREFIX_MIN}


def _eh_parte_valida(parte: str) -> bool:
    """Parte alpha-only, 3-15 chars, >=1 vogal."""
    if not (3 <= len(parte) <= 15):
        return False
    if not parte.isalpha():
        return False
    if not any(c in VOGAIS for c in parte):
        return False
    return True


def split_concatenated(local: str) -> Optional[Tuple[str, str]]:
    """Tenta quebrar concatenacao em 2 partes via transicao vogal-cons-vogal.

    Procura posicao i onde local[i-1]=vogal, local[i]=consoante, local[i+1]=vogal.
    Corte em i: local[:i] (1a parte termina antes da consoante) + local[i:].
    Retorna (parte1, parte2) ou None.
    """
    n = len(local)
    if n < 6:
        return None
    for i in range(3, n - 2):
        if local[i - 1] in VOGAIS and local[i] not in VOGAIS and local[i + 1] in VOGAIS:
            p1, p2 = local[:i], local[i:]
            if 3 <= len(p1) <= 10 and 3 <= len(p2) <= 10 and p1.isalpha() and p2.isalpha():
                return p1, p2
    return None


def _classificar_partes(partes):
    """Lookup de cada parte. Retorna 'placeholder'/'setor' ou None."""
    for p in partes:
        if p in PLACEHOLDERS_LOCAL:
            return "placeholder"
        if p in _SETORIAIS_TOTAL:
            return "setor"
    return None


def classificar_local_part(local: str) -> str:
    """Classifica local-part em uma de 4 categorias.

    Retorna: 'pessoa' | 'setor' | 'placeholder' | 'indefinido'
    """
    if not local:
        return "indefinido"
    local = local.lower().strip()

    # 1. Exact match
    if local in PLACEHOLDERS_LOCAL:
        return "placeholder"
    if local in _SETORIAIS_TOTAL:
        return "setor"

    # 2. Suffix-match em PLACEHOLDERS_SUFFIX (caioinbox, johnnoreply)
    for suf in PLACEHOLDERS_SUFFIX:
        if local.endswith(suf) and len(local) > len(suf):
            return "placeholder"

    # 3. Com separador
    if any(c in local for c in "._-"):
        partes = [p for p in re.split(r"[._-]", local) if p]
        if not partes:
            return "indefinido"
        lookup = _classificar_partes(partes)
        if lookup:
            return lookup
        if all(_eh_parte_valida(p) for p in partes):
            return "pessoa"
        return "indefinido"

    # 4. Sem separador — prefix-match em setoriais len>=5
    for s in _SETORIAIS_PREFIX:
        if local.startswith(s) and len(local) > len(s):
            return "setor"

    # 5. Sem separador — split heuristico V-C-V
    split = split_concatenated(local)
    if split:
        p1, p2 = split
        lookup = _classificar_partes([p1, p2])
        if lookup:
            return lookup
        if _eh_parte_valida(p1) and _eh_parte_valida(p2):
            return "pessoa"

    return "indefinido"
