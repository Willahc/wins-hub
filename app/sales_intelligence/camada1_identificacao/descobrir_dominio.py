import re
import logging
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse, unquote
from unidecode import unidecode

log = logging.getLogger("sales_intel.descobrir_dominio")

AGREGADORES_BLOQUEADOS = {
    "wikipedia.org", "linkedin.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "tiktok.com", "gov.br",
    "cnpj.biz", "casadosdados.com.br", "econodata.com.br",
    "consultas-cnpj.com.br", "consultacnpj.com.br", "guiamais.com.br",
    "telelistas.net", "apontador.com.br", "reclameaqui.com.br",
    "jusbrasil.com.br", "yumpu.com", "issuu.com", "scribd.com",
    "google.com", "duckduckgo.com", "yahoo.com", "bing.com",
    "blogspot.com", "wordpress.com",
    "poder360.com.br", "agenciainfra.com", "conexao085.com.br",
    "jornalcana.com.br", "revistaportuaria.com.br", "imprensaoficial.com.br",
    "bnamericas.com", "valor.com.br", "infomoney.com.br", "bloomberg.com",
}

TLDS_ACEITOS = (".com.br", ".com", ".org.br", ".ind.br", ".net.br", ".net")


def _normalizar(s: str) -> str:
    s = unidecode(str(s or ""))
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def _extrair_dominio_raiz(url: str) -> Optional[str]:
    try:
        if not url.startswith("http"):
            url = "http://" + url
        host = urlparse(url).hostname or ""
        if not host:
            return None
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return None


def _eh_agregador(dominio: str) -> bool:
    return any(dominio == a or dominio.endswith("." + a) for a in AGREGADORES_BLOQUEADOS)


def _score_match(razao: str, dominio: str) -> float:
    razao_n = _normalizar(razao)
    # extrair "core" do dominio (parte antes do tld)
    core = dominio
    for tld in TLDS_ACEITOS:
        if core.endswith(tld):
            core = core[: -len(tld)]
            break
    core = _normalizar(core).replace(" ", "")
    razao_concat = razao_n.replace(" ", "")
    if not core or not razao_concat:
        return 0.0
    # substring match alto
    if core in razao_concat or razao_concat in core:
        return max(0.85, SequenceMatcher(None, core, razao_concat).ratio())
    # tentar match com primeira palavra
    primeira = razao_n.split(" ")[0] if razao_n else ""
    if primeira and primeira in core:
        return max(0.65, SequenceMatcher(None, core, razao_concat).ratio())
    return SequenceMatcher(None, core, razao_concat).ratio()


def _head_status(dominio: str) -> Optional[int]:
    import requests
    for proto in ("https", "http"):
        try:
            r = requests.head(f"{proto}://{dominio}", timeout=8, allow_redirects=True,
                              headers={"User-Agent": "WiNS Hub sales_intel"})
            return r.status_code
        except requests.RequestException:
            continue
    return None


def descobrir_dominio_via_chain(razao_social: str, nome_fantasia: Optional[str] = None):
    """Versão sem Playwright: usa SearchChain (Serper > Brave > Bing > DDG).
    Reusa filtros agregadores + TLDs aceitos + score fuzzy >=0.3.
    Retorna str (domínio) ou None. NUNCA inventa."""
    try:
        from sales_intelligence.search_engines.chain import SearchChain
    except Exception as e:
        log.warning(f"SearchChain indisponível: {e}")
        return None

    nome = nome_fantasia or razao_social
    queries = [f'"{nome}" site oficial']
    if nome_fantasia and nome_fantasia != razao_social:
        queries.append(f'"{razao_social}" site oficial')

    chain = SearchChain()
    candidatos = []
    for q in queries[:2]:
        try:
            resp = chain.search(q, max_results=20)
        except Exception as e:
            log.warning(f"chain.search falhou para '{q}': {e}")
            continue
        results = getattr(resp, "results", None) or []
        seen = set()
        for r in results[:30]:
            link = getattr(r, "url", None) or getattr(r, "link", None) or (r.get("url") if isinstance(r, dict) else None) or (r.get("link") if isinstance(r, dict) else None)
            if not link:
                continue
            d = _extrair_dominio_raiz(link.strip())
            if not d or d in seen or _eh_agregador(d):
                continue
            seen.add(d)
            if not any(d.endswith(tld) for tld in TLDS_ACEITOS):
                continue
            score = _score_match(razao_social, d)
            if nome_fantasia:
                score = max(score, _score_match(nome_fantasia, d))
            if score >= 0.5:
                candidatos.append((d, score, link))
        if candidatos:
            break

    if not candidatos:
        log.info(f"chain: nenhum candidato para '{nome}'")
        return None

    candidatos.sort(key=lambda x: -x[1])
    melhor_dom, melhor_score, melhor_url = candidatos[0]
    log.info(f"chain: melhor dom='{melhor_dom}' score={melhor_score:.2f} url='{melhor_url[:80]}'")
    return melhor_dom


def descobrir_dominio_oficial(razao_social: str, nome_fantasia: Optional[str] = None):
    """Busca DDG, filtra agregadores, calcula score fuzzy + HEAD.
    Retorna DominioOficial ou None. NUNCA inventa."""
    from playwright.sync_api import sync_playwright

    nome = nome_fantasia or razao_social
    queries = [f'"{nome}" site oficial']
    if nome_fantasia and nome_fantasia != razao_social:
        queries.append(f'"{razao_social}" site oficial')

    candidatos = []  # list of (dominio, score, snippet_url)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = b.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/147.0.0.0")
        for q in queries[:2]:
            page = ctx.new_page()
            url = f"https://html.duckduckgo.com/html/?q={q.replace(' ', '+').replace('\"', '%22')}"
            try:
                r = page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                raw = page.content()
            except Exception as e:
                log.warning(f"DDG falhou: {e}")
                page.close()
                continue
            # extrair urls de resultados
            urls = re.findall(r'href="(?:https?://duckduckgo\.com/l/\?uddg=)?(https?%3[Aa]%2F%2F[^"&]+)', raw)
            urls = [unquote(u) for u in urls]
            urls += re.findall(r'class="result__url"[^>]*>([^<]+)', raw)
            seen = set()
            for u in urls[:30]:
                d = _extrair_dominio_raiz(u.strip())
                if not d or d in seen or _eh_agregador(d):
                    continue
                seen.add(d)
                if not any(d.endswith(tld) for tld in TLDS_ACEITOS):
                    continue
                score = _score_match(razao_social, d)
                if nome_fantasia:
                    score = max(score, _score_match(nome_fantasia, d))
                if score >= 0.3:
                    candidatos.append((d, score, u))
            page.close()
            if candidatos:
                break
        b.close()

    if not candidatos:
        log.info(f"Nenhum candidato para '{nome}'")
        return None

    # melhor candidato
    candidatos.sort(key=lambda x: -x[1])
    melhor = candidatos[0]
    dominio = melhor[0]
    score = melhor[1]
    head = _head_status(dominio)

    if score >= 0.7 and head and 200 <= head < 300:
        conf = "alta"
    elif score >= 0.5 or (head and 300 <= head < 400):
        conf = "media"
    elif score >= 0.3:
        conf = "baixa"
    else:
        return None

    from sales_intelligence.models.empresa_dossier import DominioOficial
    return DominioOficial(
        dominio=dominio,
        confianca=conf,
        fonte=f"ddg:{melhor[2][:120]}",
        score_fuzzy=round(score, 3),
        head_status=head,
    )
