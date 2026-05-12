"""Integracao Camada 3 (decisores) -> Camada 4 (email) - PATTERN-FIRST.

Estrategia (economia de creditos Hunter):
  1. Resolver pattern da empresa 1x (cache 180d, Camada 2)
  2. Pra cada decisor, gerar email via pattern + nome
  3. Validar via SMTP (Camada 4 sem Hunter fallback)
  4. Se SMTP decisivo (verified_smtp/invalid) -> aceitar
  5. Se SMTP inconclusivo (greylisted/catch_all) -> AI Hunter como tiebreaker
  6. Se pattern nao existe ou confianca=baixa -> Hunter finder direto

Hunter so eh chamado:
  - quando SMTP inconclusivo (greylisted/catch_all)
  - OU quando pattern indisponivel/fraco
Em pipeline tipico (pattern OK + maioria SMTP decisivo): ~80% economia
de creditos comparado a "Hunter primeiro pra todos".

Anti-alucinacao (4 gates):
  - Pattern confianca='baixa' -> nao gera (fallback Hunter, se possivel)
  - Hunter score < 70 -> nao confiar
  - SMTP 'invalid' decisivo -> email_status='invalid' (nao tenta Hunter)
  - SMTP 'greylisted'/'catch_all' -> Hunter tiebreaker, NAO marca invalid sozinho
"""
import logging
import sys
from pathlib import Path
from typing import List, Optional

_APP = str(Path(__file__).resolve().parents[1])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada2_pattern_detection.gerar_email import gerar_email
from sales_intelligence.camada2_pattern_detection.resolver_pattern import (
    resolver_pattern_para_dominio,
)
from sales_intelligence.camada4_email_validacao.hunter_adapter import (
    encontrar_email as hunter_find,
)
from sales_intelligence.camada4_email_validacao.orquestrador import validar_email
from sales_intelligence.models.decisor import Decisor

log = logging.getLogger("sales_intel.integracao_c3_c4")

HUNTER_SCORE_MIN = 70


def _via_hunter_finder(decisor: Decisor, dominio: str) -> Optional[str]:
    """Hunter email-finder (consome credito). So usar quando necessario."""
    data = hunter_find(decisor.nome_pessoa, dominio)
    if not data or not data.get("email"):
        return None
    score = data.get("score") or 0
    if score < HUNTER_SCORE_MIN:
        log.info(f"hunter score baixo ({score}) p/ {decisor.nome_pessoa}")
        return None
    return data["email"].lower()


def enriquecer_decisores_com_email(
    cnpj: str,
    dominio_oficial: str,
    decisores: List[Decisor],
    pattern=None,           # se ja resolveu fora, passa aqui; senao resolve internamente
    permitir_hunter: bool = True,
) -> List[Decisor]:
    """Enriquecimento pattern-first. Retorna decisores com email+email_status preenchidos.

    `permitir_hunter=False` desliga Hunter (util pra testes/economia)."""
    if not (dominio_oficial and decisores):
        return decisores

    # 1) Resolver pattern uma unica vez (cache 180d)
    if pattern is None:
        pattern = resolver_pattern_para_dominio(dominio_oficial)

    pattern_usavel = pattern is not None and getattr(pattern, "confianca", "baixa") != "baixa"
    log.info(f"pattern usavel={pattern_usavel} dominio={dominio_oficial} "
             f"(padrao={pattern.padrao if pattern else None}, "
             f"conf={pattern.confianca if pattern else None})")

    out: List[Decisor] = []
    stats = {"verified_smtp": 0, "invalid": 0, "via_pattern": 0,
             "via_hunter": 0, "hunter_skip_indisponivel": 0,
             "sem_email": 0, "via_smtp_hunter_fallback": 0}

    for d in decisores:
        novo = d.model_copy()
        email_candidato: Optional[str] = None
        fonte_geracao: Optional[str] = None

        # 2) Pattern-first: tentar gerar via pattern
        if pattern_usavel:
            email_candidato = gerar_email(d.nome_pessoa, pattern, dominio_oficial)
            if email_candidato:
                email_candidato = email_candidato.lower()
                fonte_geracao = "pattern"
                stats["via_pattern"] += 1

        # 3) Sem pattern -> Hunter finder como fallback
        if not email_candidato and permitir_hunter:
            email_candidato = _via_hunter_finder(d, dominio_oficial)
            if email_candidato:
                fonte_geracao = "hunter_finder"
                stats["via_hunter"] += 1

        if not email_candidato:
            novo.email = None
            novo.email_status = None
            stats["sem_email"] += 1
            out.append(novo)
            continue

        # 4) Validar via SMTP (SEM Hunter fallback inicial pra economia)
        try:
            v = validar_email(email_candidato, usar_hunter_fallback=False)
        except Exception as e:
            log.warning(f"validar_email exception {email_candidato}: {e}")
            v = None

        if v is None:
            novo.email = email_candidato
            novo.email_status = "pending"
            stats["sem_email"] += 1
            out.append(novo)
            continue

        # 5) SMTP decisivo -> aceitar
        if v.status in ("verified_smtp", "invalid"):
            novo.email = email_candidato
            novo.email_status = v.status
            stats[v.status] += 1
            log.info(f"  {d.nome_pessoa[:28]:28s} -> {email_candidato} ({v.status} via {fonte_geracao})")
            out.append(novo)
            continue

        # 6) SMTP inconclusivo (greylisted/catch_all) -> Hunter verifier tiebreaker
        if permitir_hunter and v.status in ("greylisted", "catch_all"):
            v2 = validar_email(email_candidato, usar_hunter_fallback=True)
            novo.email = email_candidato
            novo.email_status = v2.status
            stats["via_smtp_hunter_fallback"] += 1
            log.info(f"  {d.nome_pessoa[:28]:28s} -> {email_candidato} ({v2.status} via SMTP+Hunter)")
            out.append(novo)
            continue

        # Hunter desligado e SMTP inconclusivo -> mapear pra 'pending' (constraint DB)
        novo.email = email_candidato
        # SMTP retorna 'greylisted'/'catch_all' que NÃO são valores válidos do check constraint
        # do DB. Mapear pra 'pending' preserva info de que email existe mas validação incompleta.
        novo.email_status = 'pending' if v.status in ('greylisted', 'catch_all') else v.status
        out.append(novo)

    log.info(f"enriquecimento C3->C4 stats: {stats}")

    # 7) Persistir cache (best-effort)
    try:
        from sales_intelligence.db.cache_decisores import gravar_decisor
        for d in out:
            if d.email:
                gravar_decisor(d)
    except Exception as e:
        log.debug(f"persist falhou: {e}")

    return out
