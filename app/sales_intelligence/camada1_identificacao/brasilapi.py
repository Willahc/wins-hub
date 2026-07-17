import re
import time
import logging
from typing import Optional
import requests

log = logging.getLogger("sales_intel.brasilapi")

# Token bucket simples (25 req/min)
_BUCKET = {"tokens": 25.0, "last": time.monotonic(), "rate": 25 / 60.0, "max": 25.0}


def _acquire_token():
    now = time.monotonic()
    elapsed = now - _BUCKET["last"]
    _BUCKET["tokens"] = min(_BUCKET["max"], _BUCKET["tokens"] + elapsed * _BUCKET["rate"])
    _BUCKET["last"] = now
    if _BUCKET["tokens"] < 1:
        wait = (1 - _BUCKET["tokens"]) / _BUCKET["rate"]
        time.sleep(wait)
        _BUCKET["tokens"] = 0.0
        _BUCKET["last"] = time.monotonic()
    else:
        _BUCKET["tokens"] -= 1


def normalizar_cnpj(cnpj_raw: str) -> str:
    value = "" if cnpj_raw is None else str(cnpj_raw)
    return re.sub(r"\D", "", value)


def validar_cnpj_dv(cnpj: str) -> bool:
    c = normalizar_cnpj(cnpj)
    if len(c) != 14 or not c.isdigit():
        return False
    if c == c[0] * 14:
        return False
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    p2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    s = sum(int(c[i]) * p1[i] for i in range(12))
    r = s % 11
    if int(c[12]) != (0 if r < 2 else 11 - r):
        return False
    s = sum(int(c[i]) * p2[i] for i in range(13))
    r = s % 11
    return int(c[13]) == (0 if r < 2 else 11 - r)


def _normalizar_situacao(value):
    if not isinstance(value, str):
        return value
    normalized = value.strip().upper()
    return {"ATIVO": "ATIVA", "INATIVO": "INATIVA"}.get(normalized, value)


def _formatar_interno(cnpj: str, dados: dict) -> dict:
    return {
        "cnpj": cnpj,
        "razao_social": dados.get("razao_social"),
        "nome_fantasia": dados.get("nome_fantasia"),
        "descricao_situacao_cadastral": _normalizar_situacao(dados.get("situacao")),
        "uf": dados.get("uf"),
        "municipio": dados.get("municipio"),
        "natureza_juridica": dados.get("natureza_entidade"),
        "cnae_fiscal": None,
        "cnae_fiscal_descricao": None,
        "cnaes_secundarios": [],
        "logradouro": dados.get("endereco"),
        "capital_social": None,
        "identificador_matriz_filial": None,
    }


def _mesclar_interno(dados: dict, payload: dict) -> dict:
    merged = dict(payload)
    mapping = {
        "razao_social": "razao_social",
        "nome_fantasia": "nome_fantasia",
        "situacao": "descricao_situacao_cadastral",
        "uf": "uf",
        "municipio": "municipio",
        "endereco": "logradouro",
        "natureza_entidade": "natureza_juridica",
    }
    for internal_field, payload_field in mapping.items():
        value = dados.get(internal_field)
        if value is not None and str(value).strip():
            if internal_field == "situacao":
                value = _normalizar_situacao(value)
            merged[payload_field] = value
    return merged


def _registrar_externo(result, *, error=None, duration_ms=None):
    if not result:
        return
    try:
        from services.cnpj_master_resolver import record_external_lookup
        record_external_lookup(
            result,
            provider="BrasilAPI",
            external_executed=True,
            external_avoided=False,
            error=error,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        log.warning("Falha ao registrar BrasilAPI no lookup mestre: %s", exc)


def buscar_dados_cnpj(cnpj: str) -> Optional[dict]:
    """Consulta BrasilAPI com resolvedor interno prioritario."""
    c = normalizar_cnpj(cnpj)
    lookup_result = None
    internal_data = {}
    try:
        from services.cnpj_master_resolver import resolve_cnpj
        context = {
            "origem_da_solicitacao": "sales_intelligence_brasilapi",
            "contexto": "buscar_dados_cnpj",
            "provedor_externo": "BrasilAPI",
        }
        lookup_result = resolve_cnpj(
            cnpj,
            required_fields=[
                "razao_social",
                "situacao",
                "cnae_fiscal",
                "natureza_juridica",
                "capital_social",
                "identificador_matriz_filial",
            ],
            context=context,
        )
        status = lookup_result.get("status")
        if status in {"INVALID", "SEM_CNPJ", "CPF_NAO_APLICAVEL"}:
            raise ValueError("CNPJ invalido (DV1+DV2)")
        if status == "FULL_HIT":
            return _formatar_interno(
                lookup_result["cnpj_normalizado"],
                lookup_result.get("dados_encontrados") or {},
            )
        if status == "PARTIAL_HIT":
            internal_data = lookup_result.get("dados_encontrados") or {}
        c = lookup_result.get("cnpj_normalizado") or c
    except ValueError:
        raise
    except Exception as exc:
        log.warning("Erro no resolvedor interno (sales_intelligence): %s", exc)

    # Mesmo com falha tecnica no resolvedor, CNPJ invalido nunca chega a rede.
    if not validar_cnpj_dv(c):
        raise ValueError("CNPJ invalido (DV1+DV2)")

    url = f"https://brasilapi.com.br/api/cnpj/v1/{c}"
    waits = [1, 4]
    started = time.monotonic()
    last_error = None
    for attempt in range(3):
        _acquire_token()
        try:
            response = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "WiNS Hub sales_intel"},
            )
            if response.status_code == 404:
                duration_ms = (time.monotonic() - started) * 1000
                _registrar_externo(
                    lookup_result,
                    error="HTTP 404",
                    duration_ms=duration_ms,
                )
                log.info("BrasilAPI 404 cnpj=%s", c)
                if internal_data:
                    return _formatar_interno(c, internal_data)
                return None
            if response.status_code == 200:
                try:
                    payload = response.json()
                except (TypeError, ValueError) as exc:
                    last_error = f"invalid_json:{type(exc).__name__}"
                    log.warning("BrasilAPI JSON invalido cnpj=%s: %s", c, exc)
                else:
                    duration_ms = (time.monotonic() - started) * 1000
                    _registrar_externo(
                        lookup_result,
                        duration_ms=duration_ms,
                    )
                    return _mesclar_interno(internal_data, payload)
            else:
                last_error = f"HTTP {response.status_code}"
                log.warning(
                    "BrasilAPI HTTP %s cnpj=%s attempt=%s",
                    response.status_code,
                    c,
                    attempt + 1,
                )
        except requests.RequestException as exc:
            last_error = f"request_error:{type(exc).__name__}"
            log.warning(
                "BrasilAPI exception cnpj=%s attempt=%s: %s",
                c,
                attempt + 1,
                exc,
            )
        if attempt < len(waits):
            time.sleep(waits[attempt])
    duration_ms = (time.monotonic() - started) * 1000
    _registrar_externo(
        lookup_result,
        error=last_error or "sem_resposta_util",
        duration_ms=duration_ms,
    )
    if internal_data:
        return _formatar_interno(c, internal_data)
    return None


def mapear_para_dossier(dados: dict) -> Optional[dict]:
    if not dados:
        return None
    cap = dados.get("capital_social")
    cap_f: Optional[float] = None
    if cap is not None:
        try:
            cap_f = float(cap)
        except (TypeError, ValueError):
            cap_f = None
    matriz_raw = dados.get("identificador_matriz_filial")
    matriz = None if matriz_raw in (None, "") else str(matriz_raw) == "1"
    return {
        "cnpj": str(dados.get("cnpj") or ""),
        "razao_social": dados.get("razao_social"),
        "nome_fantasia": dados.get("nome_fantasia") or None,
        "natureza_juridica": dados.get("natureza_juridica"),
        "cnae_fiscal": str(dados.get("cnae_fiscal") or "") or None,
        "cnae_descricao": dados.get("cnae_descricao") if "cnae_descricao" in dados else dados.get("cnae_fiscal_descricao"),
        "situacao_cadastral": dados.get("descricao_situacao_cadastral") or dados.get("situacao_cadastral"),
        "uf": dados.get("uf"),
        "municipio": dados.get("municipio"),
        "capital_social": cap_f,
        "matriz": matriz,
    }
