import logging
import sys
from pathlib import Path
from typing import Optional

# garantir que sales_intelligence.* funciona quando rodado fora do container
_APP_PATH = str(Path(__file__).resolve().parents[2])
if _APP_PATH not in sys.path:
    sys.path.insert(0, _APP_PATH)

from sales_intelligence.models.empresa_dossier import EmpresaDossier, DominioOficial, WhoisInfo
from sales_intelligence.camada1_identificacao import brasilapi, descobrir_dominio, subdominios, whois_rdap, classificar

log = logging.getLogger("sales_intel.orquestrador")


def _calcular_confianca_geral(dossier: EmpresaDossier) -> float:
    # ponderacao: brasilapi 0.4, dominio 0.3 (peso pela confianca), whois 0.15, subdomains 0.1, classif 0.05
    score = 0.0
    fontes = []
    if dossier.razao_social:
        score += 0.4
        fontes.append("brasilapi")
    if dossier.dominio_oficial:
        peso_conf = {"alta": 1.0, "media": 0.65, "baixa": 0.35}.get(dossier.dominio_oficial.confianca, 0)
        score += 0.30 * peso_conf
        fontes.append(f"dominio_{dossier.dominio_oficial.confianca}")
    if dossier.whois and dossier.whois.fonte_metodo != "falhou":
        score += 0.15
        fontes.append(f"whois_{dossier.whois.fonte_metodo}")
    if dossier.subdominios:
        score += min(0.10, 0.02 * len(dossier.subdominios))
        fontes.append(f"crtsh_{len(dossier.subdominios)}")
    score += 0.05 * dossier.confianca_classificacao
    dossier.fontes_utilizadas = fontes
    return round(min(score, 1.0), 3)


def identificar_empresa(cnpj: str, force_refresh: bool = False) -> EmpresaDossier:
    """Pipeline completo Camada 1.
    Levanta ValueError se CNPJ invalido. Retorna EmpresaDossier sempre."""
    cnpj_clean = brasilapi.normalizar_cnpj(cnpj)
    if not brasilapi.validar_cnpj_dv(cnpj_clean):
        raise ValueError(f"CNPJ {cnpj} invalido (DV)")

    # 1) cache (best-effort, falha silenciosa)
    if not force_refresh:
        try:
            from sales_intelligence.db.cache_dossier import buscar as buscar_cache
            cached = buscar_cache(cnpj_clean)
            if cached:
                log.info(f"cache hit cnpj={cnpj_clean}")
                return cached
        except Exception as e:
            log.debug(f"cache lookup falhou: {e}")

    # 2) BrasilAPI bloqueante
    dados = brasilapi.buscar_dados_cnpj(cnpj_clean)
    if not dados:
        raise RuntimeError(f"BrasilAPI nao retornou dados para {cnpj_clean}")
    base = brasilapi.mapear_para_dossier(dados)

    # 3) classificacao com o que ja tem
    classif = classificar.classificar_organizacao(base)
    base.update(classif)

    # 4) descobrir dominio (best-effort)
    dominio_obj: Optional[DominioOficial] = None
    try:
        if base.get("razao_social"):
            dominio_obj = descobrir_dominio.descobrir_dominio_oficial(
                base["razao_social"], base.get("nome_fantasia")
            )
    except Exception as e:
        log.warning(f"descobrir_dominio falhou: {e}")

    # 5) se dominio, buscar subdominios + whois (best-effort)
    subs: list = []
    whois_data: Optional[dict] = None
    if dominio_obj:
        try:
            subs = subdominios.listar_subdominios(dominio_obj.dominio)
        except Exception as e:
            log.warning(f"subdominios falhou: {e}")
        try:
            whois_data = whois_rdap.buscar_whois(dominio_obj.dominio)
        except Exception as e:
            log.warning(f"whois_rdap falhou: {e}")

    # 6) montar EmpresaDossier
    dossier = EmpresaDossier(
        **base,
        dominio_oficial=dominio_obj,
        subdominios=subs,
        whois=WhoisInfo(**whois_data) if whois_data else None,
    )
    dossier.confianca_geral = _calcular_confianca_geral(dossier)

    # 7) gravar cache (best-effort)
    try:
        from sales_intelligence.db.cache_dossier import gravar as gravar_cache
        gravar_cache(dossier)
    except Exception as e:
        log.debug(f"cache write falhou: {e}")

    return dossier
