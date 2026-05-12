"""Cache de validacoes de email. TTL curto (30 dias) por design."""
import logging
from datetime import datetime, timedelta
from typing import Optional
from psycopg2.extras import RealDictCursor

from sales_intelligence.db import get_conn

log = logging.getLogger("sales_intel.cache_email_validacao")


def buscar(email: str):
    from sales_intelligence.models.email_validacao import EmailValidacao
    if not email:
        return None
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT email, status, sintaxe_ok, mx_record,
                           smtp_response_code, smtp_response_msg, catch_all,
                           fonte_validacao, confianca, validated_at
                      FROM email_validacao_cache
                     WHERE email = %s
                       AND (proxima_revalidacao IS NULL OR proxima_revalidacao > CURRENT_DATE)
                """, (email.lower(),))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return EmailValidacao(**row)
    except Exception as e:
        log.debug(f"cache buscar falhou {email}: {e}")
        return None


def gravar(validacao, validade_dias: int = 30) -> bool:
    try:
        proxima = (datetime.utcnow() + timedelta(days=validade_dias)).date()
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO email_validacao_cache (
                        email, status, sintaxe_ok, mx_record,
                        smtp_response_code, smtp_response_msg, catch_all,
                        fonte_validacao, confianca, validated_at, proxima_revalidacao
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                    ON CONFLICT (email) DO UPDATE SET
                        status = EXCLUDED.status,
                        sintaxe_ok = EXCLUDED.sintaxe_ok,
                        mx_record = EXCLUDED.mx_record,
                        smtp_response_code = EXCLUDED.smtp_response_code,
                        smtp_response_msg = EXCLUDED.smtp_response_msg,
                        catch_all = EXCLUDED.catch_all,
                        fonte_validacao = EXCLUDED.fonte_validacao,
                        confianca = EXCLUDED.confianca,
                        validated_at = NOW(),
                        proxima_revalidacao = EXCLUDED.proxima_revalidacao
                """, (validacao.email.lower(), validacao.status, validacao.sintaxe_ok,
                      validacao.mx_record, validacao.smtp_response_code,
                      validacao.smtp_response_msg, validacao.catch_all,
                      validacao.fonte_validacao, validacao.confianca, proxima))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.debug(f"cache gravar falhou: {e}")
        return False
