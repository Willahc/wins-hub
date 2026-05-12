"""Camada 3 orquestrador: integra 4 fontes, dedup por nome+cnpj, score, cache."""
import logging
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from unidecode import unidecode

from sales_intelligence.camada1_identificacao.brasilapi import normalizar_cnpj, validar_cnpj_dv
from sales_intelligence.camada3_decisores import (
    linkedin_search, crea_search, cvm_ipe, dou_search,
)
from sales_intelligence.camada3_decisores.mapping import determinar_nivel
from sales_intelligence.models.decisor import Decisor, DecisorBruto

log = logging.getLogger("sales_intel.orquestrador_decisores")

CONF_RANK = {"alta": 3, "media": 2, "baixa": 1}
FONTE_RANK = {"cvm": 4, "dou": 3, "ddg": 2, "bing": 2, "google": 2, "crea": 1}


def _chave_pessoa(nome: str) -> str:
    return unidecode((nome or "").lower().strip())


def _consolidar(brutos: List[DecisorBruto], cnpj: str) -> List[Decisor]:
    """Dedup por nome normalizado, eleva confianca quando multiplas fontes batem."""
    by_chave: dict = {}
    fontes_secundarias: dict = {}

    for b in brutos:
        chave = _chave_pessoa(b.nome_pessoa)
        # registrar fonte (TODAS, incluindo a primeira)
        fontes_secundarias.setdefault(chave, set()).add(b.fonte_descoberta)
        if chave in by_chave:
            existing_conf = CONF_RANK.get(by_chave[chave].confianca, 0)
            new_conf = CONF_RANK.get(b.confianca, 0)
            existing_fonte = FONTE_RANK.get(by_chave[chave].fonte_descoberta, 0)
            new_fonte = FONTE_RANK.get(b.fonte_descoberta, 0)
            # manter o mais forte como primaria
            if new_fonte > existing_fonte or (
                new_fonte == existing_fonte and new_conf > existing_conf
            ):
                by_chave[chave] = b
        else:
            by_chave[chave] = b

    consolidados = []
    for chave, b in by_chave.items():
        outros = fontes_secundarias.get(chave, set()) - {b.fonte_descoberta}
        fonte_sec = sorted(outros)[0] if outros else None
        # se ha multiplas fontes, eleva confianca
        conf = b.confianca
        if outros and conf == "media":
            conf = "alta"
        elif outros and conf == "baixa":
            conf = "media"

        tipo_final = b.tipo_cargo or "OUTRO"
        nivel = b.cargo_nivel or determinar_nivel(tipo_final)

        # score_relevancia
        base = {"tatico": 0.7, "operacional": 0.4, "estrategico": 0.3}.get(nivel or "estrategico", 0.3)
        bonus = 0.0
        if conf == "alta":
            bonus += 0.2
        if b.tipo_cargo and b.tipo_cargo != "OUTRO":
            bonus += 0.1
        if b.linkedin_slug:
            bonus += 0.1
        score = round(min(base + bonus, 1.0), 2)

        consolidados.append(Decisor(
            cnpj=cnpj,
            nome_pessoa=b.nome_pessoa,
            cargo_raw=b.cargo_raw,
            cargo_normalizado=b.cargo_normalizado,
            tipo_cargo=tipo_final,
            cargo_idioma=b.cargo_idioma,
            cargo_nivel=nivel,
            confianca=conf,
            fonte_descoberta=b.fonte_descoberta,
            fonte_secundaria=fonte_sec,
            snippet_origem=b.snippet_origem,
            url_origem=b.url_origem,
            linkedin_slug=b.linkedin_slug,
            score_relevancia=score,
            descoberto_em=datetime.utcnow(),
            revalidacao=date.today() + timedelta(days=180),
        ))

    return sorted(consolidados, key=lambda d: -d.score_relevancia)


def descobrir_decisores(cnpj: str, empresa_nome: str,
                        force_refresh: bool = False,
                        max_buckets_linkedin: int = 3,
                        habilitar_cvm: bool = True,
                        habilitar_dou: bool = True,
                        habilitar_crea: bool = True) -> List[Decisor]:
    cnpj_clean = normalizar_cnpj(cnpj)
    if not validar_cnpj_dv(cnpj_clean):
        raise ValueError(f"CNPJ {cnpj} invalido")

    if not force_refresh:
        try:
            from sales_intelligence.db.cache_decisores import buscar_por_cnpj
            cached = buscar_por_cnpj(cnpj_clean)
            if cached:
                log.info(f"cache hit cnpj={cnpj_clean} n={len(cached)}")
                return cached
        except Exception as e:
            log.debug(f"cache lookup falhou: {e}")

    brutos: List[DecisorBruto] = []

    # LinkedIn (primaria)
    try:
        brutos += linkedin_search.descobrir_via_search_engines(
            empresa_nome, cnpj_clean, max_buckets=max_buckets_linkedin)
    except Exception as e:
        log.warning(f"linkedin_search falhou: {e}")

    # CREA (secundaria - engenheiros)
    if habilitar_crea:
        try:
            brutos += crea_search.descobrir_via_crea(cnpj_clean, empresa_nome)
        except Exception as e:
            log.warning(f"crea_search falhou: {e}")

    # CVM IPE (secundaria - capital aberto)
    if habilitar_cvm:
        try:
            brutos += cvm_ipe.descobrir_via_cvm(cnpj_clean)
        except Exception as e:
            log.warning(f"cvm_ipe falhou: {e}")

    # DOU (secundaria - estatais)
    if habilitar_dou:
        try:
            brutos += dou_search.descobrir_via_dou(empresa_nome)
        except Exception as e:
            log.warning(f"dou_search falhou: {e}")

    consolidados = _consolidar(brutos, cnpj_clean)

    # gravar cache (best-effort)
    try:
        from sales_intelligence.db.cache_decisores import gravar_decisor
        for d in consolidados:
            gravar_decisor(d)
    except Exception as e:
        log.debug(f"cache write em batch falhou: {e}")

    return consolidados
