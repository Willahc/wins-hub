"""Camada 3 - busca primaria de decisores via LinkedIn (search engine chain).

Usa SearchChain (Brave -> Bing -> DDG por default). Resultados normalizados
em SearchResult; parser extrai nome+cargo+slug do snippet/raw_html.
"""
import logging
import re
import time
from html import unescape
from typing import List, Optional

from unidecode import unidecode

from sales_intelligence.search_engines.chain import default_chain
from sales_intelligence.camada3_decisores.mapping import (
    QUERY_BUCKETS, normalizar_cargo, detectar_idioma, determinar_nivel,
)
from sales_intelligence.camada3_decisores.temporal_gate import detectar_ex_funcionario
from sales_intelligence.models.decisor import DecisorBruto

log = logging.getLogger("sales_intel.linkedin_search")

# Termos comerciais que aparecem em snippets LinkedIn como se fossem nomes.
# Filtramos pra evitar criar "decisor" com nome = "Sourcing Specialist".
BLACKLIST_NOMES = {
    "sourcing", "supply chain", "procurement", "buyer",
    "engineering", "operations", "maintenance", "manager",
    "director", "engenharia", "compras", "suprimentos",
    "manutencao", "operacoes", "diretor", "gerente",
    "coordenador", "coordinator", "head", "chief", "officer",
    "grupo", "group", "holding", "ltda", "s.a.", "s/a",
    "company", "empresa", "organization", "team",
}


def _validar_nome(nome: str, empresa_nome: str) -> bool:
    """Rejeita nomes que sao termos comerciais ou capturam empresa/frase."""
    if not nome:
        return False
    nome_lower = unidecode(nome.lower().strip())
    # palavra unica em blacklist
    if nome_lower in BLACKLIST_NOMES:
        return False
    # primeira palavra OU primeiras 2 (concat) na blacklist (ex.: "Supply Chain Manager")
    partes = nome_lower.split()
    if not partes:
        return False
    if partes[0] in BLACKLIST_NOMES:
        return False
    if len(partes) >= 2 and " ".join(partes[:2]) in BLACKLIST_NOMES:
        return False
    # contem nome da empresa (parser capturou label da empresa)
    emp_low = unidecode((empresa_nome or "").lower())
    if emp_low and len(emp_low) >= 4 and emp_low in nome_lower:
        return False
    # < 2 ou > 5 palavras
    n_partes = len(nome.split())
    if n_partes < 2 or n_partes > 5:
        return False
    # contem digitos
    if any(c.isdigit() for c in nome):
        return False
    return True

LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com(?:/[a-z][a-z])?/in/([a-zA-Z0-9._-]+)")
# Mantido para backward compat (test_camada3); prod usa detectar_ex_funcionario (mais robusto)
TEMPORAL_HISTORICO = re.compile(
    r"\b(ex-|former|trabalhou\s+como|worked\s+as|previously|anteriormente)\b",
    re.IGNORECASE,
)


def _construir_query(termos: List[str], empresa_nome: str) -> str:
    """Query aberta com keyword 'linkedin' em vez de site: operator.
    Google deindexa parcialmente LinkedIn em site: queries; abertura
    melhora dramatically recall mantendo precision (filtramos URL no parser).
    """
    or_part = " OR ".join(f'"{t}"' for t in termos)
    return f'({or_part}) "{empresa_nome}" linkedin'


def _extrair_da_url_e_snippet(url: str, title: str, snippet: str, raw_html: str = ""):
    """Extrai (nome, cargo, empresa_validacao, slug) de um SearchResult.
    Heuristica: titulo do LinkedIn = 'Nome Sobrenome - Cargo - Empresa | LinkedIn'.
    """
    slug_m = LINKEDIN_SLUG_RE.search(url) or LINKEDIN_SLUG_RE.search(raw_html)
    if not slug_m:
        return None
    slug = slug_m.group(1)

    # texto combinado pra parser
    texto = f"{title} {snippet}"
    texto = re.sub(r"\s+", " ", unescape(texto)).strip()

    # padrao: "Nome Sobrenome - Cargo at Empresa" / "Nome Sobrenome - Cargo - Empresa"
    nome = cargo = emp = None
    m = re.match(
        r"([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç']+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç']+){1,4})\s*[-–|]\s*([^|·-]+?)\s*(?:\bat\b|\bna\b|\bem\b|[-–|])\s*([^|·]+?)(?:\s*[|·]\s*LinkedIn)?$",
        texto,
        re.IGNORECASE,
    )
    if m:
        nome = m.group(1).strip()
        cargo = m.group(2).strip().rstrip(".,;")
        emp = m.group(3).strip()
    else:
        # so nome (sem cargo/empresa parseaveis)
        m2 = re.search(
            r"([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç']+(?:\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç']+){1,4})",
            texto,
        )
        if m2:
            nome = m2.group(1).strip()

    if not nome or len(nome.split()) < 2:
        return None
    return {
        "nome": nome,
        "cargo": cargo or "",
        "empresa_validacao": emp or "",
        "linkedin_slug": slug,
        "snippet": (title + " | " + snippet)[:500],
    }


def _proximity_check(snippet: str, cargo: str, empresa_nome: str) -> bool:
    """True se cargo e empresa coexistem no snippet com distancia razoavel."""
    if not (snippet and cargo and empresa_nome):
        return False
    s = snippet.lower()
    c = cargo.lower()[:40]
    e = empresa_nome.lower()[:40]
    pc = s.find(c)
    pe = s.find(e)
    if pc < 0 or pe < 0:
        return False
    return abs(pc - pe) <= 200


def descobrir_via_search_engines(empresa_nome: str, cnpj: Optional[str] = None,
                                  max_buckets: int = 3) -> List[DecisorBruto]:
    """Descobre decisores via chain de search engines (Brave -> Bing -> DDG).
    max_buckets limita quantas queries OR rodam (~10 termos por query)."""
    if not empresa_nome:
        return []

    decisores: dict = {}
    buckets = QUERY_BUCKETS[:max_buckets]

    for idx, bucket in enumerate(buckets):
        query = _construir_query(bucket, empresa_nome)
        log.info(f"bucket {idx+1}/{len(buckets)} ({len(bucket)} termos)")
        resp = default_chain.search(query, max_results=20)
        if not resp.results:
            log.info(f"  bucket {idx+1} sem resultados (engine={resp.engine}, status={resp.status})")
            continue

        for sr in resp.results:
            cand = _extrair_da_url_e_snippet(sr.url, sr.title, sr.snippet, sr.raw_html)
            if not cand:
                continue
            # blacklist: rejeita termos comerciais como "nome"
            if not _validar_nome(cand["nome"], empresa_nome):
                log.debug(f"rejeitado blacklist: {cand['nome']!r}")
                continue
            snippet_full = sr.snippet + " " + cand["snippet"]
            # filtro temporal v2 - metadata-only, NAO descarta (Claude C5 eh gate efetivo).
            # Smoke retro 11/05 mostrou recall regex = 2.9% e FP alta = 33%
            # (Rodrigo Correa production-ready descartado por confusao Embratel range vs
            # Petrobras 'desde' no mesmo snippet). Marca apenas como metadata pro C5.
            gate = detectar_ex_funcionario(snippet_full)
            marcadores = gate.padrao_match if gate.eh_ex else None
            if gate.eh_ex:
                log.info(f"temporal_gate marcou {cand['nome']!r} "
                         f"padrao={gate.padrao_match} conf={gate.confianca} "
                         f"(metadata only, Claude decide)")
            # proximity (anti-alucinacao)
            if cand["cargo"] and not _proximity_check(snippet_full, cand["cargo"], empresa_nome):
                continue

            cargo_raw = cand["cargo"] or ""
            tipo = normalizar_cargo(cargo_raw)
            idioma = detectar_idioma(cargo_raw)
            nivel = determinar_nivel(tipo)

            empresa_lower = empresa_nome.lower()
            emp_val_lower = (cand["empresa_validacao"] or "").lower()
            bate_empresa = (
                empresa_lower in emp_val_lower
                or emp_val_lower in empresa_lower
                or empresa_lower in snippet_full.lower()
            )
            bate_cargo = bool(tipo) and tipo != "OUTRO"
            if bate_empresa and bate_cargo:
                conf = "alta"
            elif bate_empresa or bate_cargo:
                conf = "media"
            else:
                conf = "baixa"

            chave = cand["nome"].lower().strip()
            if chave in decisores:
                conf_rank = {"alta": 3, "media": 2, "baixa": 1}
                if conf_rank.get(conf, 0) <= conf_rank.get(decisores[chave].confianca, 0):
                    continue

            decisores[chave] = DecisorBruto(
                nome_pessoa=cand["nome"],
                cargo_raw=cargo_raw,
                cargo_normalizado=cargo_raw if tipo else None,
                tipo_cargo=tipo,
                cargo_idioma=idioma,
                cargo_nivel=nivel,
                linkedin_slug=cand["linkedin_slug"],
                snippet_origem=snippet_full[:500],
                url_origem=sr.url[:300],
                confianca=conf,
                fonte_descoberta=resp.engine,  # 'brave'/'bing'/'ddg'
                marcadores_temporais=marcadores,
            )
        time.sleep(3)  # anti-rate-limit suave

    log.info(f"linkedin_search concluido: {len(decisores)} pessoas unicas")
    return list(decisores.values())
