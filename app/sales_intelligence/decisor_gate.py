"""Gate de inserção de decisores — defesa contra replicação falsa.

Histórico: enrichment_19_05 inseriu 2.716 decisores via LinkedIn search,
dos quais 1.246 eram replicações falsas (Eduardo Ayres em 184 SPEs solares,
etc.). Sprint 3 introduziu este gate como checkpoint único antes de QUALQUER
INSERT em decisores_obra.

Regra de ouro: todo novo código que insere em decisores_obra DEVE chamar
decisor_inserivel() antes. Sem exceção. Ver SKILL.md em
.claude/skills/decisor-capture/ pra checklist de wire-in.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from unidecode import unidecode


# Gate 0: cargos categoricamente não-compradores (investidores, sócios, board)
CARGOS_NAO_DECISOR = (
    "partner", "investor", "advisor", "conselheiro",
    "board member", "sócio investidor", "venture",
    "fund manager", "managing director",  # ambíguo mas em PE/VC indica investidor
    "boardroom", "non-executive",
)

# Sufixos corp que ruído pra normalizar empresa
_SUFIXOS_CORP = {
    "sa", "s/a", "s.a", "s.a.", "ltda", "ltd", "eireli",
    "grupo", "group", "holding", "companhia", "co",
    "brasil", "brazil", "br",
    "participacoes", "participações", "part",
    "do", "da", "de", "dos", "das", "e",
    "industria", "indústria", "industrias", "indústrias", "industrial",
    "agro", "agronegocio", "agronegócio",
    "com", "corp", "corporation", "inc",
    "saneamento", "energia", "energias", "energetica", "energética",
    "logistica", "logística", "transportes", "transp",
    "aeroportos", "aeroporto", "spe", "empreendimentos",
    "geracao", "geração", "renovaveis", "renováveis",
}

# Threshold do Gate 2 — quantos CNPJ-raízes DISTINTOS o (nome, cargo) já tem.
# Se for <= isto, ainda é considerado "decisor único no cluster" e passa o gate.
# Acima disso → dispersão estatística vira replicação falsa provável.
GATE2_THRESHOLD_RAIZES = 2


def _normalize(s: Optional[str]) -> str:
    return unidecode((s or "").lower())


def _tokens_distintivos(empresa: str, min_len: int = 5) -> list[str]:
    norm = re.sub(r"[^a-z0-9\s]", " ", _normalize(empresa))
    tokens = [t for t in norm.split() if t not in _SUFIXOS_CORP and len(t) >= min_len]
    if not tokens:
        tokens = [t for t in norm.split() if t not in _SUFIXOS_CORP and len(t) >= 3]
    return tokens


def _cargo_cita_empresa(cargo: str, empresa: str) -> bool:
    """True se algum token distintivo da empresa aparece no cargo."""
    if not cargo or not empresa:
        return False
    cargo_norm = _normalize(cargo)
    for tok in _tokens_distintivos(empresa):
        if re.search(rf"\b{re.escape(tok)}\b", cargo_norm):
            return True
    return False


def _qtd_cnpj_raizes_existentes(cur, nome: str, cargo: str) -> int:
    """Conta CNPJ-raízes distintos onde (nome, cargo) já está inserido (não excluído).

    Compat com tuple cursor e RealDictCursor — usa AS qtd + fallback indexed.
    """
    cur.execute(
        """
        SELECT COUNT(DISTINCT LEFT(regexp_replace(COALESCE(o.cnpj, ''), '[^0-9]', '', 'g'), 8)) AS qtd
          FROM decisores_obra d
          JOIN obras o ON o.id = d.obra_id
         WHERE d.excluido_em IS NULL
           AND lower(d.nome) = lower(%s)
           AND lower(COALESCE(d.cargo, '')) = lower(COALESCE(%s, ''))
           AND COALESCE(o.cnpj, '') <> ''
        """,
        (nome, cargo),
    )
    row = cur.fetchone()
    if not row:
        return 0
    val = row["qtd"] if isinstance(row, dict) else row[0]
    return int(val) if val is not None else 0


def decisor_inserivel(
    cur,
    nome: str,
    cargo: str,
    empresa_obra: str,
    cnpj_raiz_obra: Optional[str] = None,
) -> Tuple[bool, str]:
    """Gate centralizado pré-INSERT em decisores_obra.

    Args:
        cur: psycopg2 cursor já aberto (lê DB pra Gate 2).
        nome: nome do decisor candidato.
        cargo: cargo do decisor candidato.
        empresa_obra: obra.empresa (string descritiva da empresa).
        cnpj_raiz_obra: 8 dígitos do CNPJ-raiz da obra (opcional, melhora Gate 2).

    Returns:
        (permite, motivo) — motivo é string curta pra log/auditoria.

    Ordem dos gates:
      Gate 0 — cargo é categoricamente não-decisor (Partner/Investor/...) → rejeita
      Gate 1 — cargo cita token distintivo da empresa → aceita (match defensável)
      Gate 2 — (nome, cargo) já em ≤ GATE2_THRESHOLD_RAIZES CNPJ-raízes → aceita
               (decisor único no cluster, ainda não é replicação)
      Default → rejeita como replicação genérica provável
    """
    nome = (nome or "").strip()
    cargo = (cargo or "").strip()
    empresa_obra = (empresa_obra or "").strip()

    if not nome or not empresa_obra:
        return False, "input_invalido"

    cargo_lower = cargo.lower()
    for nd in CARGOS_NAO_DECISOR:
        if nd in cargo_lower:
            return False, f"cargo_nao_decisor:{nd}"

    if _cargo_cita_empresa(cargo, empresa_obra):
        return True, "cargo_cita_empresa"

    raizes_atuais = _qtd_cnpj_raizes_existentes(cur, nome, cargo)
    if raizes_atuais <= GATE2_THRESHOLD_RAIZES:
        return True, f"decisor_unico_cluster:raizes={raizes_atuais}"

    return False, f"cargo_generico_multi_grupo:raizes={raizes_atuais}"
