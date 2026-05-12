"""Resolve EmailPattern por dominio: cache-first, fallback coleta + deteccao + grava.

Pattern por empresa eh cacheado 180 dias (validade longa porque padroes
corporativos mudam pouco).
"""
import logging
from typing import Optional

log = logging.getLogger("sales_intel.resolver_pattern")

# Coletores isolados: cada um falha sozinho sem derrubar os outros.
# (Playwright pode estar ausente; sem PAT do GitHub roda com rate limit baixo;
#  Hunter domain-search queima 1 credito por chamada.)
try:
    from sales_intelligence.camada2_pattern_detection.coletar_emails_site import (
        coletar_emails_do_site as _coletar_site,
    )
    # probe playwright (modulo importa lazy, mas coletor retorna [] sem playwright)
    import playwright.sync_api  # noqa: F401
    SITE_AVAILABLE = True
except ImportError as e:
    log.warning(f"coletor site desabilitado: {e}")
    SITE_AVAILABLE = False

try:
    from sales_intelligence.camada2_pattern_detection.coletar_emails_github import (
        coletar_emails_github as _coletar_github,
    )
    GITHUB_AVAILABLE = True
except ImportError as e:
    log.warning(f"coletor github desabilitado: {e}")
    GITHUB_AVAILABLE = False

try:
    from services.hunter import buscar_emails_dominio as _hunter_domain
    HUNTER_DOMAIN_AVAILABLE = True
except ImportError as e:
    log.warning(f"coletor hunter_domain desabilitado: {e}")
    HUNTER_DOMAIN_AVAILABLE = False

try:
    from sales_intelligence.camada2_pattern_detection.coletar_emails_serper import (
        coletar_emails_via_serper as _coletar_serper,
    )
    SERPER_AVAILABLE = True
except ImportError as e:
    log.warning(f"coletor serper desabilitado: {e}")
    SERPER_AVAILABLE = False

from sales_intelligence.camada2_pattern_detection import detectar_padrao


def resolver_pattern_para_dominio(dominio: str, force_refresh: bool = False):
    """Retorna EmailPattern ou None.

    Fluxo:
      1. Cache lookup (empresa_email_pattern_cache, TTL 180d)
      2. Cache miss: coletar de cada fonte disponivel (best-effort)
      3. Detectar padrao dominante (>= 3 emails -> alta)
      4. Persistir no cache
    """
    if not dominio:
        return None

    # 1. Cache
    if not force_refresh:
        try:
            from sales_intelligence.db.cache_pattern import buscar_pattern_cache
            cached = buscar_pattern_cache(dominio)
            if cached:
                log.info(f"pattern cache hit dominio={dominio} padrao={cached.padrao}")
                return cached
        except Exception as e:
            log.debug(f"cache lookup falhou: {e}")

    # 2. Coletar de cada fonte disponivel. Ordem: serper (rico, queima ~3 creditos
    # Serper) -> github (free) -> hunter_domain (queima credito Hunter) -> site
    # (depende de playwright).
    emails: list = []
    coletores = []
    if SERPER_AVAILABLE:
        coletores.append(("serper", lambda d=dominio: [e for e, _ in (_coletar_serper(d) or [])]))
    if GITHUB_AVAILABLE:
        coletores.append(("github", lambda d=dominio: _coletar_github(d)))
    if HUNTER_DOMAIN_AVAILABLE:
        def _hd(d=dominio):
            h = _hunter_domain(d) or {}
            data = h.get("data") if isinstance(h.get("data"), dict) else None
            items = (data.get("emails") if data else h.get("emails")) or []
            out = []
            for item in items:
                em = item.get("value") if isinstance(item, dict) else None
                if em:
                    out.append(em.lower())
            return out
        coletores.append(("hunter_domain", _hd))
    if SITE_AVAILABLE:
        coletores.append(("site", lambda d=dominio: _coletar_site(d)))

    for nome, fn in coletores:
        try:
            novos = fn() or []
            emails += novos
            log.info(f"  coletor {nome}: {len(novos)} emails p/ {dominio}")
        except Exception as e:
            log.warning(f"  coletor {nome} falhou {dominio}: {e}")

    if not emails:
        log.info(f"sem emails coletados p/ {dominio}, pattern indisponivel")
        return None

    pattern = detectar_padrao.detectar_padrao(list(set(emails)))
    if not pattern:
        log.info(f"pattern inconclusivo p/ {dominio} (amostra={len(set(emails))})")
        return None

    # 3. Persistir cache
    try:
        from sales_intelligence.db.cache_pattern import gravar_pattern_cache
        gravar_pattern_cache(pattern)
    except Exception as e:
        log.debug(f"pattern cache gravar falhou: {e}")

    log.info(f"pattern detectado dominio={dominio} padrao={pattern.padrao} conf={pattern.confianca}")
    return pattern
