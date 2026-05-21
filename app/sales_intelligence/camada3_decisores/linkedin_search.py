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
PREPOSICOES_BR = {"em", "no", "na", "para", "com", "de", "da", "do", "dos", "das", "pelos", "pelas", "pelo", "pela", "num", "numa"}

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
    partes_pre = nome.strip().split()
    if partes_pre and partes_pre[0].lower() in PREPOSICOES_BR:
        return False
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
    """Query via site:linkedin.com/in (Serper primary).
    Field test 18/05/2026 (Sprint 4): site:linkedin.com/in via Serper supera
    keyword-based dramatically. Mantém keyword 'linkedin' como sufixo pra fallback
    engines (Brave/Bing/DDG não indexam site: bem mas trabalham keyword).
    """
    or_part = " OR ".join(f'"{t}"' for t in termos)
    return f'({or_part}) "{empresa_nome}" site:linkedin.com/in'


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
                                  max_buckets: int = 6) -> List[DecisorBruto]:
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
            # v3 20/05: bate_empresa STRICT — empresa_extraida (do title LK) deve
            # bater empresa-buscada. Bug pré-v3: snippet-only match falso-positivava
            # (ex: Beatriz Itau BBA virou ADECOAGRO porque snippet mencionou ADECOAGRO).
            bate_empresa = (
                empresa_lower in emp_val_lower
                or emp_val_lower in empresa_lower
            )
            # Token overlap fallback (>= 4 chars distinctivos) — pega
            # "ADECOAGRO BRASIL" vs "Adecoagro" mesmo com sufixos diferentes.
            if not bate_empresa and emp_val_lower:
                emp_search_tokens = {
                    t for t in empresa_lower.replace("-", " ").split()
                    if len(t) >= 4 and t not in ("ltda","sociedade","empresa","grupo","holding")
                }
                emp_extraida_tokens = set(emp_val_lower.replace("-", " ").split())
                if emp_search_tokens & emp_extraida_tokens:
                    bate_empresa = True
            mention_only = (not bate_empresa) and (empresa_lower in snippet_full.lower())
            bate_cargo = bool(tipo) and tipo != "OUTRO"
            if bate_empresa and bate_cargo:
                conf = "alta"
            elif bate_empresa:
                conf = "media"
            elif mention_only and bate_cargo:
                conf = "baixa"  # downgrade: só snippet mencionou empresa
            elif bate_cargo:
                conf = "baixa"
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



def descobrir_via_tecnica_mari(
    empresa_nome: str,
    cnpj: Optional[str] = None,
    max_cargos: int = 6,
) -> List[DecisorBruto]:
    """Tecnica Mari (2 passos):
    Passo 1: busca '"empresa" "cargo"' SEM linkedin -> extrai nomes dos snippets
             (Google traz sites corporativos, releases, noticias com nome real)
    Passo 2: busca '"nome" "empresa" linkedin' -> confirma perfil + cargo
    Complementa descobrir_via_search_engines como fonte adicional.
    """
    if not empresa_nome:
        return []

    CARGOS_PASSO1 = [
        "supply chain", "capex", "suprimentos", "compras",
        "gerente de projetos", "engenharia", "investimentos",
        "diretor industrial", "coordenador de obras",
    ][:max_cargos]

    nomes_encontrados = {}  # nome -> cargo_raw_contexto

    # PASSO 1: extrair nomes via busca empresa+cargo (sem linkedin)
    nome_re = re.compile(
        r'\b([A-Z\u00C0-\u00DC][a-z\u00E0-\u00FC\']+(?:\s+[A-Z\u00C0-\u00DC][a-z\u00E0-\u00FC\']+){1,3})\s*[,\-\u2013]\s*([^,\n]{5,60})'
    )
    for cargo in CARGOS_PASSO1:
        query = f'"{empresa_nome}" "{cargo}"'
        try:
            resp = default_chain.search(query, max_results=10)
            for sr in resp.results:
                snippet = f"{sr.title} {sr.snippet}"
                for nome, cargo_ctx in nome_re.findall(snippet):
                    if not _validar_nome(nome, empresa_nome):
                        continue
                    # Fix: usar palavras significativas do nome (>3 chars), exigir >=50% match
                    palavras_empresa = [p.lower() for p in empresa_nome.split() if len(p) > 3]
                    match_empresa = sum(1 for p in palavras_empresa if p in snippet.lower())
                    if not palavras_empresa or match_empresa < max(1, len(palavras_empresa) // 2):
                        continue
                    if nome not in nomes_encontrados:
                        nomes_encontrados[nome] = cargo_ctx.strip()
        except Exception as e:
            log.warning(f"tecnica_mari passo1 cargo='{cargo}': {e}")
        time.sleep(1)

    if not nomes_encontrados:
        log.info(f"tecnica_mari: passo1 0 nomes pra '{empresa_nome}'")
        return []

    # PASSO 2: confirmar cada nome no LinkedIn
    decisores: List[DecisorBruto] = []
    for nome, cargo_ctx in list(nomes_encontrados.items())[:8]:
        query_li = f'"{nome}" "{empresa_nome}" linkedin'
        try:
            resp = default_chain.search(query_li, max_results=5)
            for sr in resp.results:
                if 'linkedin.com/in/' not in sr.url:
                    continue
                slug = sr.url.split('linkedin.com/in/')[-1].split('/')[0].split('?')[0]
                tipo = normalizar_cargo(cargo_ctx)
                nivel = determinar_nivel(tipo) if tipo else "tatico"
                idioma = detectar_idioma(cargo_ctx)
                # Fix 3: slug deve conter pelo menos 1 token (>2 chars) do nome
                tokens_nome = [t.lower() for t in nome.split() if len(t) > 2]
                if not any(t in slug.lower() for t in tokens_nome):
                    continue
                cargo_limpo = cargo_ctx.split('.')[0].split('\n')[0].strip()[:60]
                # Rejeitar cargo_raw que parece extrato de bio/perfil LinkedIn
                RUIDO_BIO = ['graduado', 'linkedin', 'formado', 'possui', 'atua', 'especialista', 'experiencia em']
                if any(r in cargo_limpo.lower() for r in RUIDO_BIO):
                    continue
                decisores.append(DecisorBruto(
                    nome_pessoa=nome,
                    cargo_raw=cargo_limpo,
                    cargo_normalizado=cargo_ctx if tipo else None,
                    tipo_cargo=tipo,
                    cargo_idioma=idioma,
                    cargo_nivel=nivel,
                    linkedin_slug=slug,
                    snippet_origem=(sr.snippet or "")[:500],
                    url_origem=sr.url[:300],
                    confianca="media",
                    fonte_descoberta="tecnica_mari_2passos",
                ))
                break
        except Exception as e:
            log.warning(f"tecnica_mari passo2 nome='{nome}': {e}")
        time.sleep(1)

    log.info(f"tecnica_mari: passo1={len(nomes_encontrados)} nomes, passo2={len(decisores)} confirmados")
    return decisores
