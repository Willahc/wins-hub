"""Camada 4 orquestrador: pipeline sintaxe -> MX -> SMTP probe -> Hunter fallback.

Estrategia:
1. validar_sintaxe (filtra lixo malformado)
2. tem_mx (filtra dominios sem mailserver)
3. is_catch_all (se sim, SMTP probe nao eh confiavel; vai direto pro Hunter)
4. smtp_probe (RCPT TO probe; conservador em greylisting)
5. fallback Hunter verifier (se status=greylisted ou catch_all)
"""
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sales_intelligence.camada4_email_validacao.validar_sintaxe import validar_sintaxe
from sales_intelligence.camada4_email_validacao.validar_mx import tem_mx
from sales_intelligence.camada4_email_validacao.validar_smtp import smtp_probe
from sales_intelligence.camada4_email_validacao.detectar_catch_all import is_catch_all
from sales_intelligence.camada4_email_validacao.hunter_adapter import (
    verificar_email as hunter_verify, encontrar_email as hunter_find,
    hunter_disponivel, mapear_status_hunter,
)
from sales_intelligence.models.email_validacao import EmailValidacao, EmailEncontrado

log = logging.getLogger("sales_intel.orquestrador_email")


def validar_email(email: str, usar_hunter_fallback: bool = True) -> EmailValidacao:
    """Pipeline completo. Sempre retorna EmailValidacao (status reflete resultado)."""
    # 1. sintaxe
    ok, dominio = validar_sintaxe(email)
    if not ok:
        return EmailValidacao(email=email, status="invalid", sintaxe_ok=False,
                               fonte_validacao="sintaxe", confianca="alta")

    # 2. MX
    has_mx, mx_host = tem_mx(dominio)
    if not has_mx:
        return EmailValidacao(email=email, status="invalid", sintaxe_ok=True,
                               fonte_validacao="mx", confianca="alta")

    # 3. catch-all check (cache 24h por dominio)
    catch_all = is_catch_all(dominio)
    if catch_all:
        # SMTP nao eh confiavel; tentar Hunter
        if usar_hunter_fallback and hunter_disponivel():
            return _via_hunter_verifier(email, mx_host, sintaxe_ok=True, catch_all=True)
        return EmailValidacao(email=email, status="catch_all", sintaxe_ok=True,
                               mx_record=mx_host, catch_all=True,
                               fonte_validacao="mx_catch_all", confianca="baixa")

    # 4. SMTP probe
    smtp_status, smtp_code, smtp_msg = smtp_probe(email, mx_host)
    if smtp_status == "verified_smtp":
        return EmailValidacao(email=email, status="verified_smtp", sintaxe_ok=True,
                               mx_record=mx_host, smtp_response_code=smtp_code,
                               smtp_response_msg=smtp_msg, fonte_validacao="smtp",
                               confianca="alta")
    if smtp_status == "invalid":
        return EmailValidacao(email=email, status="invalid", sintaxe_ok=True,
                               mx_record=mx_host, smtp_response_code=smtp_code,
                               smtp_response_msg=smtp_msg, fonte_validacao="smtp",
                               confianca="alta")

    # 5. greylisted -> Hunter fallback
    if usar_hunter_fallback and hunter_disponivel():
        return _via_hunter_verifier(email, mx_host, sintaxe_ok=True, catch_all=False)
    return EmailValidacao(email=email, status="greylisted", sintaxe_ok=True,
                           mx_record=mx_host, smtp_response_code=smtp_code,
                           smtp_response_msg=smtp_msg, fonte_validacao="smtp",
                           confianca="baixa")


def _via_hunter_verifier(email: str, mx_host: str, sintaxe_ok: bool,
                          catch_all: bool) -> EmailValidacao:
    data = hunter_verify(email)
    if not data:
        return EmailValidacao(email=email, status="greylisted", sintaxe_ok=sintaxe_ok,
                               mx_record=mx_host, catch_all=catch_all,
                               fonte_validacao="hunter_failed", confianca="baixa")
    hunter_result = (data.get("result") or "unknown").lower()
    score = data.get("score")
    status = mapear_status_hunter(hunter_result)
    confianca = "alta" if status in ("verified_smtp", "invalid") else "media"
    if score is not None and score < 50:
        confianca = "baixa"
    return EmailValidacao(
        email=email, status=status, sintaxe_ok=sintaxe_ok, mx_record=mx_host,
        catch_all=catch_all, fonte_validacao=f"hunter:{hunter_result}",
        confianca=confianca,
    )


def encontrar_email(nome_completo: str, dominio: str) -> Optional[EmailEncontrado]:
    """Wrapper sobre Hunter finder. Util quando gerar_email da Camada 2 nao tem
    pattern suficiente. Retorna None se Hunter nao tem hit."""
    if not hunter_disponivel():
        log.info("Hunter indisponivel, encontrar_email retorna None")
        return None
    data = hunter_find(nome_completo, dominio)
    if not data or not data.get("email"):
        return None
    return EmailEncontrado(
        nome=nome_completo, dominio=dominio,
        email=data["email"], score_hunter=data.get("score"),
        fonte="hunter_finder",
    )
