"""Gate temporal: detecta marcadores de ex-funcionario no snippet
ANTES de persistir decisor. Reduz 60-70% do ruido da Camada 3."""

import re
from typing import Optional

# Padroes regex em ordem de confianca (alta -> baixa).
# Primeira ocorrencia que casa vence (early-exit).
PATTERNS_EX_TEMPORAL = [
    # (nome_padrao, regex, confianca)
    ("range_anos_explicito",
     r"\b(19|20)\d{2}\s*[-–—]\s*(19|20)\d{2}\b",
     "alta"),

    ("ate_ano",
     r"\b(at[ée]|to)\s+(19|20)\d{2}\b",
     "alta"),

    ("ex_prefix_pt",
     r"\bex[\-\s]*(diretor|diretora|gerente|coordenador|coordenadora|chefe|presidente|engenheiro|engenheira|analista)",
     "alta"),

    ("former_prefix_en",
     r"\bformer\s+(director|manager|coordinator|chief|president|engineer|analyst|head|vp)",
     "alta"),

    ("previously_anteriormente",
     r"\b(previously|anteriormente|antiga|antigo|antes\s+em|antes\s+na)\b",
     "media"),

    ("range_meses_ano",
     r"\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[\s\-/]+(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[\s/]+(19|20)\d{2}\b",
     "alta"),

    ("range_meses_en",
     r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[\s\-]+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(19|20)\d{2}\b",
     "alta"),

    ("duracao_curta",
     r"\b\d+\s+(meses?|months?)\b",
     "media"),

    ("saiu_em_ano",
     r"\b(saiu|left|departed|deixou)\s+(em|in|a)\s+(19|20)\d{2}\b",
     "alta"),

    ("aposentado",
     r"\b(aposentado|aposentada|retired|retirou)\b",
     "alta"),
]


class TemporalGateResult:
    def __init__(self, eh_ex: bool, padrao_match: Optional[str] = None,
                 confianca: Optional[str] = None, trecho: Optional[str] = None):
        self.eh_ex = eh_ex
        self.padrao_match = padrao_match
        self.confianca = confianca
        self.trecho = trecho

    def __repr__(self):
        if self.eh_ex:
            return f"<TemporalGate EX padrao={self.padrao_match} conf={self.confianca}>"
        return "<TemporalGate ATUAL>"


def detectar_ex_funcionario(snippet: str, empresa_alvo: str = "") -> TemporalGateResult:
    """Detecta se snippet indica ex-funcionario via padroes temporais.

    Heuristica conservadora: na duvida, retorna eh_ex=False (nao exclui).
    """
    if not snippet:
        return TemporalGateResult(eh_ex=False)

    snippet_lower = snippet.lower()

    for padrao_nome, regex, confianca in PATTERNS_EX_TEMPORAL:
        match = re.search(regex, snippet_lower, re.IGNORECASE)
        if match:
            trecho = snippet[max(0, match.start() - 20):min(len(snippet), match.end() + 20)]
            return TemporalGateResult(
                eh_ex=True,
                padrao_match=padrao_nome,
                confianca=confianca,
                trecho=trecho,
            )

    return TemporalGateResult(eh_ex=False)
