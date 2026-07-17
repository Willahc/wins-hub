"""Prioritized lookup of CNPJ data in the internal master registry.

The resolver is deliberately fail-open for valid CNPJs: an unavailable internal
database must not interrupt the existing external-provider fallback. Invalid
identifiers are always rejected before the feature flag is considered.
"""

import json
import logging
import math
import os
import re
import time
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation

import psycopg2
from psycopg2.extras import RealDictCursor


log = logging.getLogger(__name__)


def _env_timeout(name, default, minimum=0, maximum=86_400_000):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


MASTER_CNPJ_LOOKUP_ENABLED = os.getenv(
    "MASTER_CNPJ_LOOKUP_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "wins_app"),
    "password": os.getenv("DB_PASSWORD", ""),
    "connect_timeout": _env_timeout(
        "DB_CONNECT_TIMEOUT", 3, minimum=1, maximum=60
    ),
    "options": "-c statement_timeout={} -c lock_timeout={}".format(
        _env_timeout("DB_STATEMENT_TIMEOUT_MS", 3000),
        _env_timeout("DB_LOCK_TIMEOUT_MS", 1000),
    ),
}

DEFAULT_REQUIRED_FIELDS = ("razao_social", "situacao")
FIELD_ALIASES = {
    "descricao_situacao_cadastral": "situacao",
    "natureza_juridica": "natureza_entidade",
    "logradouro": "endereco",
}
MASTER_DATA_FIELDS = (
    "razao_social",
    "nome_fantasia",
    "natureza_entidade",
    "situacao",
    "endereco",
    "municipio",
    "uf",
    "completude",
    "confianca",
    "fontes",
    "captadores",
    "campos_ausentes",
)
TERMINAL_STATUSES = frozenset(
    {"INVALID", "SEM_CNPJ", "CPF_NAO_APLICAVEL"}
)

_SENSITIVE_MARKER_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:password|passwd|pwd|token|secret|"
    r"api[_ -]?key|authorization|credential|credencial|senha|"
    r"client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|"
    r"private[_ -]?key|database[_ -]?url|dsn)(?:$|[^a-z0-9])|"
    r"\bBearer\s+|://[^\s:/@]+:[^\s@]+@"
)
_CPF_RE = re.compile(
    r"(?<!\d)\d{3}[.\s-]?\d{3}[.\s-]?\d{3}[-\s]?\d{2}(?!\d)"
)
_SCIENTIFIC_RE = re.compile(
    r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)[eE][+-]?\d+$"
)
_INTEGRAL_DECIMAL_TEXT_RE = re.compile(r"^\+?(\d+)[.,]0+$")
_MISSING_IDENTIFIERS = frozenset(
    {
        "", "-", "--", "n/a", "na", "nan", "none", "null", "sem cnpj",
        "nao informado", "nao aplicavel",
    }
)


def _numeric_identifier_digits(value):
    """Convert an integral numeric representation without keeping its exponent."""
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return format(number, "f").partition(".")[0]


def _identifier_digits(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return _numeric_identifier_digits(value)
    if isinstance(value, (int, Decimal)):
        return _numeric_identifier_digits(value)

    text = str(value).strip()
    marker = unicodedata.normalize("NFKD", text.casefold())
    marker = "".join(char for char in marker if not unicodedata.combining(char))
    if marker in _MISSING_IDENTIFIERS:
        return ""
    integral_decimal = _INTEGRAL_DECIMAL_TEXT_RE.fullmatch(text)
    if integral_decimal:
        # Preserve the textual integer part exactly, including explicit leading
        # zeroes. Never infer missing zeroes from a shorter numeric value.
        return integral_decimal.group(1)
    if _SCIENTIFIC_RE.fullmatch(text):
        return _numeric_identifier_digits(text.replace(",", "."))
    if re.search(r"[A-Za-z]", text):
        return None
    return re.sub(r"\D", "", text)


def _normalize_cnpj(value) -> str:
    """Return a safe digit representation without retaining exponent digits."""
    return _identifier_digits(value) or ""


def _classify_identifier(value):
    digits = _identifier_digits(value)
    if digits == "":
        return "SEM_CNPJ", None
    if digits is not None and len(digits) == 11:
        return "CPF_NAO_APLICAVEL", None
    if digits is None or not is_valid_cnpj(digits):
        return "INVALID", digits
    return None, digits


def is_valid_cnpj(cnpj: str) -> bool:
    """Validate both CNPJ check digits after removing formatting characters."""
    normalized = _normalize_cnpj(cnpj)
    if len(normalized) != 14 or not normalized.isdigit():
        return False
    if normalized == normalized[0] * 14:
        return False

    def check_digit(base, weights):
        remainder = sum(
            int(digit) * weight for digit, weight in zip(base, weights)
        ) % 11
        return 0 if remainder < 2 else 11 - remainder

    first = check_digit(
        normalized[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    )
    second = check_digit(
        normalized[:13], (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    )
    return normalized[-2:] == f"{first}{second}"


def _sanitize_log_text(value, max_length=1000):
    """Remove common secret and CPF representations before persistence."""
    if value is None:
        return None
    text = str(value).replace("\x00", " ")
    if _SENSITIVE_MARKER_RE.search(text):
        return "<redacted-sensitive-message>"
    text = _CPF_RE.sub("<redacted-cpf>", text)
    text = " ".join(text.split())
    return text[:max_length]


def _safe_error(error):
    if error is None:
        return None
    if isinstance(error, BaseException):
        value = f"{type(error).__name__}: {error}"
    else:
        value = str(error)
    return _sanitize_log_text(value)


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _normalize_required_fields(required_fields):
    if required_fields is None:
        values = DEFAULT_REQUIRED_FIELDS
    elif isinstance(required_fields, str):
        values = (required_fields,)
    else:
        try:
            values = tuple(required_fields)
        except TypeError:
            values = (required_fields,)

    normalized = []
    seen = set()
    for value in values:
        field = _sanitize_log_text(value, max_length=100)
        if field:
            field = field.strip().lower()
            field = FIELD_ALIASES.get(field, field)
        if not field or field in seen:
            continue
        seen.add(field)
        normalized.append(field)
    return normalized or list(DEFAULT_REQUIRED_FIELDS)


def _context_values(context):
    if isinstance(context, dict):
        contexto = context.get("contexto", "resolucao_cnpj")
        origem = context.get("origem_da_solicitacao", "api")
        provider = context.get("provedor_externo")
        read_only = _as_bool(context.get("read_only", False))
        write_log = (
            _as_bool(context.get("write_log", True), default=True)
            and not read_only
        )
    else:
        contexto = context if context is not None else "resolucao_cnpj"
        origem = "api"
        provider = None
        read_only = False
        write_log = True
    return {
        "contexto": _sanitize_log_text(contexto, max_length=250)
        or "resolucao_cnpj",
        "origem_da_solicitacao": _sanitize_log_text(origem, max_length=250)
        or "api",
        "provedor_externo": _sanitize_log_text(provider, max_length=100),
        "read_only": read_only,
        "write_log": write_log,
    }


def _has_value(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _json_field_list(values):
    safe_values = []
    for value in values or ():
        safe = _sanitize_log_text(value, max_length=100)
        if safe:
            safe_values.append(safe)
    return json.dumps(safe_values, ensure_ascii=True, separators=(",", ":"))


def _duration_ms(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(number, 86_400_000.0))


def _result(
    *,
    status,
    cnpj_normalizado,
    required_fields,
    request_id,
    context_meta,
    dados_encontrados=None,
    campos_encontrados=None,
    campos_ausentes=None,
    motivo,
    entidade_id=None,
):
    dados = dados_encontrados if dados_encontrados is not None else None
    found = list(campos_encontrados or ())
    missing = list(campos_ausentes or ())
    return {
        "status": status,
        "cnpj_normalizado": cnpj_normalizado,
        "dados_encontrados": dados,
        "campos_solicitados": list(required_fields),
        "campos_encontrados": found,
        "campos_ausentes": missing,
        "fontes_internas": dados.get("fontes") if dados else None,
        "captadores": dados.get("captadores") if dados else None,
        "confianca": dados.get("confianca") if dados else None,
        "entidade_id": str(entidade_id) if entidade_id is not None else None,
        "consulta_externa_necessaria": status in {"PARTIAL_HIT", "MISS"},
        "motivo": motivo,
        "request_id": request_id,
        "lookup_id": None,
        "log_registrado": False,
        "_lookup_meta": {
            "contexto": context_meta["contexto"],
            "origem_da_solicitacao": context_meta["origem_da_solicitacao"],
            "provedor_externo": context_meta.get("provedor_externo"),
            "read_only": context_meta.get("read_only", False),
            "write_log": context_meta["write_log"],
        },
    }


def _log_cnpj(result):
    normalized = result.get("cnpj_normalizado")
    return normalized if is_valid_cnpj(normalized) else None


def _insert_lookup_log(
    result,
    *,
    provider=None,
    external_executed=False,
    external_avoided=False,
    duration_ms=0.0,
    error=None,
    motivo=None,
):
    meta = result.get("_lookup_meta") or {}
    if not _as_bool(meta.get("write_log", True), default=True):
        return False

    request_id = result.get("request_id")
    try:
        request_id = str(uuid.UUID(str(request_id)))
    except (TypeError, ValueError, AttributeError):
        request_id = str(uuid.uuid4())
        result["request_id"] = request_id

    if provider is None:
        provider = meta.get("provedor_externo")
    provider = _sanitize_log_text(provider, max_length=100)
    if _as_bool(external_executed) and not provider:
        provider = "nao_informado"

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO wins_v2.enrichment_lookup_log (
                    timestamp, request_id, cnpj_normalizado, contexto,
                    origem_da_solicitacao, status_lookup, campos_solicitados,
                    campos_encontrados, campos_ausentes, provedor_externo,
                    chamada_externa_executada, chamada_externa_evitada,
                    motivo, custo_estimado_evitado, tempo_lookup_ms, erro
                ) VALUES (
                    NOW(), %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    request_id,
                    _log_cnpj(result),
                    _sanitize_log_text(meta.get("contexto"), max_length=250),
                    _sanitize_log_text(
                        meta.get("origem_da_solicitacao"), max_length=250
                    ),
                    result.get("status"),
                    _json_field_list(result.get("campos_solicitados")),
                    _json_field_list(result.get("campos_encontrados")),
                    _json_field_list(result.get("campos_ausentes")),
                    provider,
                    _as_bool(external_executed),
                    _as_bool(external_avoided),
                    _sanitize_log_text(motivo or result.get("motivo")),
                    0.10 if _as_bool(external_avoided) else 0.0,
                    _duration_ms(duration_ms),
                    _safe_error(error),
                ),
            )
        conn.commit()
        return True
    except Exception as exc:
        # Logging is best-effort and must never change lookup/fallback behavior.
        log.warning(
            "Nao foi possivel registrar auditoria do lookup de CNPJ (%s)",
            type(exc).__name__,
        )
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_cnpj(cnpj, required_fields=None, context=None):
    """Resolve a CNPJ against wins_v2.entidades_lookup before fallback.

    Terminal identifier states never need an external request. PARTIAL_HIT and
    MISS leave that decision to the existing provider integration. The latter
    must call record_external_lookup with the actual outcome.
    """
    started_at = time.monotonic()
    terminal_status, normalized = _classify_identifier(cnpj)
    requested = _normalize_required_fields(required_fields)
    context_meta = _context_values(context)
    request_id = str(uuid.uuid4())

    # Classification intentionally precedes the feature flag. Missing values,
    # CPFs and invalid CNPJs must never reach an external CNPJ provider.
    if terminal_status is not None:
        reasons = {
            "SEM_CNPJ": "Registro sem CNPJ informado",
            "CPF_NAO_APLICAVEL": "CPF nao se aplica ao cadastro mestre de CNPJ",
            "INVALID": "CNPJ invalido: formato ou digitos verificadores incorretos",
        }
        result = _result(
            status=terminal_status,
            cnpj_normalizado=normalized,
            required_fields=requested,
            request_id=request_id,
            context_meta=context_meta,
            campos_ausentes=requested,
            motivo=reasons[terminal_status],
        )
        result["log_registrado"] = _insert_lookup_log(
            result,
            external_executed=False,
            external_avoided=False,
            duration_ms=(time.monotonic() - started_at) * 1000.0,
        )
        return result

    if not MASTER_CNPJ_LOOKUP_ENABLED:
        result = _result(
            status="MISS",
            cnpj_normalizado=normalized,
            required_fields=requested,
            request_id=request_id,
            context_meta=context_meta,
            campos_ausentes=requested,
            motivo="Cadastro mestre interno desativado por feature flag",
        )
        result["log_registrado"] = _insert_lookup_log(
            result,
            external_executed=False,
            external_avoided=False,
            duration_ms=(time.monotonic() - started_at) * 1000.0,
        )
        return result

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        if context_meta["read_only"]:
            conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT entidade_id, cnpj_normalizado, razao_social,
                       nome_fantasia, natureza_entidade, situacao, endereco,
                       municipio, uf, completude, confianca, fontes,
                       captadores, campos_ausentes
                  FROM wins_v2.entidades_lookup
                 WHERE cnpj_normalizado = %s
                 LIMIT 1
                """,
                (normalized,),
            )
            row = cursor.fetchone()
    except psycopg2.OperationalError as exc:
        log.warning(
            "Cadastro mestre indisponivel; usando fallback externo (%s)",
            type(exc).__name__,
        )
        result = _result(
            status="MISS",
            cnpj_normalizado=normalized,
            required_fields=requested,
            request_id=request_id,
            context_meta=context_meta,
            campos_ausentes=requested,
            motivo="Cadastro mestre indisponivel; fallback externo permitido",
        )
        # Do not open a second connection just to log a database outage.
        result["log_registrado"] = False
        return result
    except Exception as exc:
        log.warning(
            "Falha na consulta ao cadastro mestre de CNPJ; usando fallback (%s)",
            type(exc).__name__,
        )
        result = _result(
            status="MISS",
            cnpj_normalizado=normalized,
            required_fields=requested,
            request_id=request_id,
            context_meta=context_meta,
            campos_ausentes=requested,
            motivo="Falha na consulta interna; fallback externo permitido",
        )
        result["log_registrado"] = _insert_lookup_log(
            result,
            external_executed=False,
            external_avoided=False,
            duration_ms=(time.monotonic() - started_at) * 1000.0,
            error=exc,
        )
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not row:
        result = _result(
            status="MISS",
            cnpj_normalizado=normalized,
            required_fields=requested,
            request_id=request_id,
            context_meta=context_meta,
            campos_ausentes=requested,
            motivo="CNPJ nao encontrado no cadastro mestre interno",
        )
        result["log_registrado"] = _insert_lookup_log(
            result,
            external_executed=False,
            external_avoided=False,
            duration_ms=(time.monotonic() - started_at) * 1000.0,
        )
        return result

    row = dict(row)
    internal_data = {field: row.get(field) for field in MASTER_DATA_FIELDS}
    found = [
        field for field in requested if _has_value(internal_data.get(field))
    ]
    missing = [field for field in requested if field not in found]
    status = "FULL_HIT" if not missing else "PARTIAL_HIT"
    if status == "FULL_HIT":
        motivo = "Cadastro mestre possui todos os campos solicitados"
    else:
        motivo = "Cadastro mestre encontrado com campos obrigatorios ausentes"

    result = _result(
        status=status,
        cnpj_normalizado=normalized,
        required_fields=requested,
        request_id=request_id,
        context_meta=context_meta,
        dados_encontrados=internal_data,
        campos_encontrados=found,
        campos_ausentes=missing,
        motivo=motivo,
        entidade_id=row.get("entidade_id"),
    )
    result["log_registrado"] = _insert_lookup_log(
        result,
        external_executed=False,
        external_avoided=status == "FULL_HIT",
        duration_ms=(time.monotonic() - started_at) * 1000.0,
    )
    return result


def record_external_lookup(
    result,
    provider,
    external_executed,
    external_avoided=False,
    error=None,
    duration_ms=None,
):
    """Record the actual external-provider outcome for a prior resolution.

    The event uses the resolver request_id so the initial decision and provider
    outcome can be correlated without reading back a generated log id.
    """
    if not isinstance(result, dict):
        return False

    executed = _as_bool(external_executed)
    avoided = _as_bool(external_avoided)
    if result.get("status") in TERMINAL_STATUSES:
        if executed or avoided:
            log.warning(
                "Evento externo recusado para estado terminal de identificador (%s)",
                result.get("status"),
            )
        return False
    if error is not None:
        motivo = "Consulta externa finalizada com erro"
    elif executed:
        motivo = "Consulta externa executada"
    elif avoided:
        motivo = "Consulta externa evitada apos decisao inicial"
    else:
        motivo = "Consulta externa nao executada"

    registered = _insert_lookup_log(
        result,
        provider=provider,
        external_executed=executed,
        external_avoided=avoided,
        duration_ms=_duration_ms(duration_ms),
        error=error,
        motivo=motivo,
    )
    result["log_registrado"] = registered
    return registered
