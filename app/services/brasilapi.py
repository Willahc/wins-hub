"""
Service de consulta CNPJ via BrasilAPI com cache, integrado com o cadastro mestre V2.
"""
import os
import json
import logging
import re
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "wins_app"),
    "password": os.getenv("DB_PASSWORD", ""),
}

BRASILAPI_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
TIMEOUT = 15
DEFAULT_REQUIRED_FIELDS = (
    "cnpj",
    "razao_social",
    "nome_fantasia",
    "descricao_situacao_cadastral",
    "natureza_juridica",
    "cnae_fiscal",
    "cnae_fiscal_descricao",
    "cnaes_secundarios",
    "qsa",
    "uf",
    "municipio",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "codigo_municipio_ibge",
    "ddd_telefone_1",
    "ddd_telefone_2",
    "email",
    "porte",
    "descricao_porte",
    "capital_social",
    "data_inicio_atividade",
    "identificador_matriz_filial",
)

_PAYLOAD_FIELD_ALIASES = {
    "situacao": ("descricao_situacao_cadastral", "situacao"),
    "endereco": ("logradouro", "endereco"),
    "telefone": ("ddd_telefone_1", "telefone_1"),
}


def _normalizar_cnpj(cnpj: str) -> str:
    """Remove tudo que nao for digito. '12.345.678/0001-99' -> '12345678000199'"""
    return re.sub(r'\D', '', str(cnpj or ''))


def _validar_cnpj(cnpj: str) -> bool:
    if len(cnpj) != 14 or not cnpj.isdigit() or cnpj == cnpj[0] * 14:
        return False

    def digit(base, weights):
        remainder = sum(int(value) * weight for value, weight in zip(base, weights)) % 11
        return 0 if remainder < 2 else 11 - remainder

    first = digit(cnpj[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = digit(cnpj[:13], (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return cnpj[-2:] == f"{first}{second}"


def _normalizar_situacao(value):
    if not isinstance(value, str):
        return value
    normalized = value.strip().upper()
    return {"ATIVO": "ATIVA", "INATIVO": "INATIVA"}.get(normalized, value)


def _payload_has_required_fields(payload: dict, required_fields) -> bool:
    if not isinstance(payload, dict):
        return False
    for field in required_fields:
        aliases = _PAYLOAD_FIELD_ALIASES.get(field, (field,))
        # A cached provider response records that the field was consulted. A
        # present key with None/empty value is therefore a valid negative result.
        if not any(alias in payload for alias in aliases):
            return False
    return True


def _record_external(result, provider, *, executed, avoided=False, error=None, duration_ms=None):
    if not result:
        return
    try:
        from services.cnpj_master_resolver import record_external_lookup
        record_external_lookup(
            result,
            provider=provider,
            external_executed=executed,
            external_avoided=avoided,
            error=error,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        log.warning("Falha ao registrar desfecho externo de CNPJ: %s", exc)


def _buscar_no_cache(conn, cnpj: str):
    """Retorna payload do cache se valido (nao expirado), senao None."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT payload FROM cache_brasilapi
            WHERE cnpj = %s AND expira_em > now()
        """, (cnpj,))
        row = cur.fetchone()
        return row['payload'] if row else None


def _salvar_no_cache(conn, cnpj: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cache_brasilapi (cnpj, payload, consultado_em, expira_em)
            VALUES (%s, %s, now(), now() + INTERVAL '90 days')
            ON CONFLICT (cnpj) DO UPDATE
            SET payload = EXCLUDED.payload,
                consultado_em = EXCLUDED.consultado_em,
                expira_em = EXCLUDED.expira_em
        """, (cnpj, json.dumps(payload)))


def _upsert_empresa_receita(conn, payload: dict):
    """Insere ou atualiza empresa em fornecedores a partir do payload BrasilAPI."""
    cnpj = _normalizar_cnpj(payload.get('cnpj', ''))
    if not cnpj:
        return

    cnaes_secundarios = [
        str(c.get('codigo', '')).replace('.', '').replace('-', '').replace('/', '')
        for c in (payload.get('cnaes_secundarios') or [])
        if c.get('codigo')
    ]
    cnae_principal = str(payload.get('cnae_fiscal', '') or '')

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fornecedores (
                cnpj, razao_social, nome_fantasia,
                cnae_principal, cnae_secundarios,
                logradouro, numero, complemento, bairro, cep,
                municipio_ibge, municipio_nome, uf,
                telefone_1, telefone_2, email,
                situacao, porte, capital_social, data_abertura,
                atualizado_em
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                now()
            )
            ON CONFLICT (cnpj) DO UPDATE SET
                razao_social     = EXCLUDED.razao_social,
                nome_fantasia    = EXCLUDED.nome_fantasia,
                cnae_principal   = EXCLUDED.cnae_principal,
                cnae_secundarios = EXCLUDED.cnae_secundarios,
                logradouro       = EXCLUDED.logradouro,
                numero           = EXCLUDED.numero,
                complemento      = EXCLUDED.complemento,
                bairro           = EXCLUDED.bairro,
                cep              = EXCLUDED.cep,
                municipio_ibge   = EXCLUDED.municipio_ibge,
                municipio_nome   = EXCLUDED.municipio_nome,
                uf               = EXCLUDED.uf,
                telefone_1       = EXCLUDED.telefone_1,
                telefone_2       = EXCLUDED.telefone_2,
                email            = EXCLUDED.email,
                situacao         = EXCLUDED.situacao,
                porte            = EXCLUDED.porte,
                capital_social   = EXCLUDED.capital_social,
                data_abertura    = EXCLUDED.data_abertura,
                atualizado_em    = now()
        """, (
            cnpj,
            payload.get('razao_social'),
            payload.get('nome_fantasia'),
            cnae_principal,
            cnaes_secundarios or None,
            payload.get('logradouro'),
            payload.get('numero'),
            payload.get('complemento'),
            payload.get('bairro'),
            (payload.get('cep') or '').replace('-', '') or None,
            payload.get('codigo_municipio_ibge'),
            payload.get('municipio'),
            payload.get('uf'),
            payload.get('ddd_telefone_1'),
            payload.get('ddd_telefone_2'),
            payload.get('email'),
            payload.get('descricao_situacao_cadastral'),
            payload.get('porte'),
            payload.get('capital_social'),
            payload.get('data_inicio_atividade'),
        ))


def _merge_payloads(internal_data: dict, external_payload: dict) -> dict:
    merged = dict(external_payload)
    mapping = {
        "razao_social": "razao_social",
        "nome_fantasia": "nome_fantasia",
        "situacao": "descricao_situacao_cadastral",
        "uf": "uf",
        "municipio": "municipio",
        "endereco": "logradouro",
        "natureza_entidade": "natureza_juridica",
    }
    for int_key, ext_key in mapping.items():
        val = internal_data.get(int_key)
        if val is not None and str(val).strip() != "":
            if int_key == "situacao":
                val = _normalizar_situacao(val)
            merged[ext_key] = val
    return merged


def _format_internal_to_external(cnpj_norm, dados_master) -> dict:
    return {
        "cnpj": cnpj_norm,
        "razao_social": dados_master.get("razao_social"),
        "nome_fantasia": dados_master.get("nome_fantasia"),
        "descricao_situacao_cadastral": _normalizar_situacao(dados_master.get("situacao")),
        "uf": dados_master.get("uf"),
        "municipio": dados_master.get("municipio"),
        "natureza_juridica": dados_master.get("natureza_entidade"),
        "cnae_fiscal": None,
        "cnae_fiscal_descricao": None,
        "cnaes_secundarios": [],
        "logradouro": dados_master.get("endereco"),
        "numero": None,
        "complemento": None,
        "bairro": None,
        "cep": None,
        "codigo_municipio_ibge": None,
        "ddd_telefone_1": None,
        "ddd_telefone_2": None,
        "email": None,
        "porte": None,
        "capital_social": None,
        "data_inicio_atividade": None
    }


def consultar_cnpj_com_erro(cnpj: str, force_refresh: bool = False,
                            required_fields=None, context: dict | None = None):
    """Consulta CNPJ, priorizando o Cadastro Mestre e preservando o fallback."""
    cnpj_norm = _normalizar_cnpj(cnpj)
    req_fields = list(required_fields or DEFAULT_REQUIRED_FIELDS)
    lookup_result = None
    internal_data = {}
    lookup_context = {
        "origem_da_solicitacao": "app_services_brasilapi",
        "contexto": "consultar_cnpj_com_erro",
        "provedor_externo": "BrasilAPI",
    }
    if context:
        lookup_context.update(context)

    try:
        from services.cnpj_master_resolver import resolve_cnpj
        lookup_result = resolve_cnpj(
            cnpj,
            required_fields=req_fields,
            context=lookup_context,
        )
        status = lookup_result.get("status")
        if status in {"INVALID", "SEM_CNPJ", "CPF_NAO_APLICAVEL"}:
            return None, "FORMATO_INVALIDO"
        if status == "FULL_HIT":
            return _format_internal_to_external(
                lookup_result["cnpj_normalizado"],
                lookup_result.get("dados_encontrados") or {},
            ), None
        if status == "PARTIAL_HIT":
            internal_data = lookup_result.get("dados_encontrados") or {}
        cnpj_norm = lookup_result.get("cnpj_normalizado") or cnpj_norm
    except Exception as exc:
        log.exception("Erro no resolvedor interno: %s", exc)

    # Barreira adicional: falha do resolvedor nunca libera CNPJ invalido para a rede.
    if not _validar_cnpj(cnpj_norm):
        log.warning("CNPJ invalido bloqueado no fallback")
        return None, "FORMATO_INVALIDO"

    def with_internal(error_type):
        payload = (
            _format_internal_to_external(cnpj_norm, internal_data)
            if internal_data else None
        )
        return payload, error_type

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = False
    except Exception as exc:
        log.warning(
            "Cache local de CNPJ indisponivel; seguindo para o provedor (%s)",
            type(exc).__name__,
        )

    try:
        if conn is not None and not force_refresh:
            try:
                cached = _buscar_no_cache(conn, cnpj_norm)
                merged_cached = (
                    _merge_payloads(internal_data, cached) if cached else None
                )
                if merged_cached and _payload_has_required_fields(
                    merged_cached, req_fields
                ):
                    log.info("CNPJ %s encontrado no cache", cnpj_norm)
                    _record_external(
                        lookup_result,
                        "cache_brasilapi",
                        executed=False,
                        avoided=True,
                    )
                    return merged_cached, None
                # End the read transaction before waiting on the HTTP provider.
                conn.rollback()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log.warning(
                    "Leitura do cache de CNPJ falhou; seguindo para o provedor (%s)",
                    type(exc).__name__,
                )

        log.info("Consultando BrasilAPI: %s", cnpj_norm)
        started = time.monotonic()
        try:
            response = requests.get(
                BRASILAPI_URL.format(cnpj=cnpj_norm),
                timeout=TIMEOUT,
            )
        except requests.exceptions.Timeout:
            duration_ms = (time.monotonic() - started) * 1000
            _record_external(
                lookup_result,
                "BrasilAPI",
                executed=True,
                error="timeout",
                duration_ms=duration_ms,
            )
            log.error("Timeout consultando %s", cnpj_norm)
            return with_internal("SERVICO_INDISPONIVEL")
        except requests.exceptions.RequestException as exc:
            duration_ms = (time.monotonic() - started) * 1000
            _record_external(
                lookup_result,
                "BrasilAPI",
                executed=True,
                error=f"request_error:{type(exc).__name__}",
                duration_ms=duration_ms,
            )
            log.error("Erro de rede consultando %s: %s", cnpj_norm, exc)
            return with_internal("ERRO_REDE")

        duration_ms = (time.monotonic() - started) * 1000
        if response.status_code == 200:
            try:
                payload = _merge_payloads(internal_data, response.json())
            except (TypeError, ValueError) as exc:
                _record_external(
                    lookup_result,
                    "BrasilAPI",
                    executed=True,
                    error=f"invalid_json:{type(exc).__name__}",
                    duration_ms=duration_ms,
                )
                log.error("Payload invalido da BrasilAPI para %s: %s", cnpj_norm, exc)
                return with_internal("ERRO_REDE")
            _record_external(
                lookup_result,
                "BrasilAPI",
                executed=True,
                duration_ms=duration_ms,
            )
            if conn is not None:
                try:
                    _salvar_no_cache(conn, cnpj_norm, payload)
                    _upsert_empresa_receita(conn, payload)
                    conn.commit()
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    log.warning(
                        "Payload BrasilAPI retornado sem persistencia local (%s)",
                        type(exc).__name__,
                    )
            return payload, None

        _record_external(
            lookup_result,
            "BrasilAPI",
            executed=True,
            error=f"HTTP {response.status_code}",
            duration_ms=duration_ms,
        )
        if response.status_code == 404:
            log.warning("CNPJ %s nao encontrado na BrasilAPI", cnpj_norm)
            return with_internal("NAO_ENCONTRADO")
        if response.status_code == 400:
            log.warning("BrasilAPI 400 (formato/DV) pra %s", cnpj_norm)
            return with_internal("FORMATO_INVALIDO")
        if response.status_code == 429:
            log.warning("BrasilAPI rate-limit pra %s", cnpj_norm)
            return with_internal("RATE_LIMIT")
        if response.status_code >= 500:
            log.error(
                "BrasilAPI %s pra %s: %s",
                response.status_code,
                cnpj_norm,
                response.text[:200],
            )
            return with_internal("SERVICO_INDISPONIVEL")
        log.error(
            "BrasilAPI status inesperado %s pra %s: %s",
            response.status_code,
            cnpj_norm,
            response.text[:200],
        )
        return with_internal("ERRO_REDE")
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log.exception("Erro inesperado consultando %s: %s", cnpj_norm, exc)
        return with_internal("ERRO_REDE")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def consultar_cnpj(cnpj: str, force_refresh: bool = False,
                   required_fields=None, context: dict | None = None) -> dict | None:
    """Wrapper backward-compatible. Descarta erro_tipo."""
    dados, _ = consultar_cnpj_com_erro(
        cnpj,
        force_refresh,
        required_fields=required_fields,
        context=context,
    )
    return dados
