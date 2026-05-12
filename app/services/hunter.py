"""
Service de busca de emails via Hunter.io.

Usa HUNTER_API_KEY do environment. Sem cache próprio — quem chama (endpoint)
é responsável por ler/gravar em decisores_cache.
"""
import os
import logging
import requests

log = logging.getLogger(__name__)

HUNTER_URL = "https://api.hunter.io/v2/domain-search"
TIMEOUT = 10
MAX_EMAILS = 10


def _normalizar_dominio(dominio: str) -> str:
    s = (dominio or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.rstrip("/")


def _chamar_hunter(dominio: str, params_extra: dict | None = None) -> dict:
    """Chama Hunter /v2/domain-search com params extras opcionais.
    Retorna dict normalizado ou {erro}."""
    if not dominio:
        return {"erro": "Domínio vazio."}

    api_key = os.getenv("HUNTER_API_KEY", "").strip()
    if not api_key:
        return {"erro": "HUNTER_API_KEY não configurada no servidor."}

    dominio_clean = _normalizar_dominio(dominio)
    params = {"domain": dominio_clean, "api_key": api_key, "limit": MAX_EMAILS}
    if params_extra:
        params.update(params_extra)

    try:
        r = requests.get(HUNTER_URL, params=params, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        return {"erro": "Timeout ao consultar Hunter.io."}
    except requests.exceptions.RequestException as e:
        log.error(f"Erro de rede Hunter.io {dominio_clean}: {e}")
        return {"erro": f"Falha de rede: {e}"}

    if r.status_code == 401:
        return {"erro": "HUNTER_API_KEY inválida ou expirada."}
    if r.status_code == 429:
        return {"erro": "Rate limit Hunter.io atingido. Tente em alguns minutos."}
    if r.status_code == 451:
        return {"erro": "Domínio bloqueado pelo Hunter.io."}
    if r.status_code != 200:
        return {"erro": f"Hunter.io retornou HTTP {r.status_code}."}

    try:
        payload = r.json()
    except ValueError:
        return {"erro": "Resposta inválida da Hunter.io."}

    data = payload.get("data") or {}
    raw_emails = data.get("emails") or []
    emails_norm = []
    for e in raw_emails[:MAX_EMAILS]:
        emails_norm.append({
            "value": e.get("value"),
            "type": e.get("type"),
            "confidence": e.get("confidence"),
            "first_name": e.get("first_name"),
            "last_name": e.get("last_name"),
            "position": e.get("position"),
            "seniority": e.get("seniority"),
            "department": e.get("department"),
        })

    meta = payload.get("meta") or {}
    total_emails_no_dominio = meta.get("results") or len(emails_norm)

    return {
        "emails": emails_norm,
        "creditos_usados": 1,
        "total_emails_no_dominio": total_emails_no_dominio,
        "dominio_pesquisado": dominio_clean,
    }


def buscar_emails_dominio(dominio: str) -> dict:
    """Busca emails de um domínio na Hunter.io (sem filtro de departamento)."""
    return _chamar_hunter(dominio)


def buscar_emails_management(dominio: str) -> dict:
    """Busca emails de um domínio filtrando ?department=management (C-level / managers)."""
    return _chamar_hunter(dominio, {"department": "management"})


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    if len(sys.argv) < 2:
        print("Uso: python hunter.py <dominio>")
        sys.exit(1)
    res = buscar_emails_dominio(sys.argv[1])
    if "erro" in res:
        print(f"ERRO: {res['erro']}")
    else:
        print(f"OK — {len(res['emails'])} emails encontrados (créditos usados: {res['creditos_usados']})")
        for e in res["emails"]:
            print(f"  {e['value']} ({e.get('position') or 'sem cargo'})")
