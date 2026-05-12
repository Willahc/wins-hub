import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("sales_intel.cache_dossier")

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def _conn():
    return psycopg2.connect(**DB)


def buscar(cnpj: str):
    """Retorna EmpresaDossier do cache se valido, senao None."""
    try:
        from sales_intelligence.models.empresa_dossier import EmpresaDossier
        conn = _conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT payload, proxima_revalidacao
                      FROM empresa_dossier_cache
                     WHERE cnpj = %s
                       AND (proxima_revalidacao IS NULL OR proxima_revalidacao > CURRENT_DATE)
                """, (cnpj,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return EmpresaDossier.model_validate(row["payload"])
    except Exception as e:
        log.debug(f"cache buscar falhou cnpj={cnpj}: {e}")
        return None


def gravar(dossier, validade_dias: int = 30) -> bool:
    """UPSERT do dossier no cache. Validade padrao 30 dias."""
    try:
        payload = dossier.model_dump(mode="json") if hasattr(dossier, "model_dump") else dict(dossier)
        cnpj = payload.get("cnpj")
        if not cnpj:
            return False
        proxima = (datetime.utcnow() + timedelta(days=validade_dias)).date()
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO empresa_dossier_cache
                      (cnpj, payload, coletado_em, proxima_revalidacao)
                    VALUES (%s, %s::jsonb, NOW(), %s)
                    ON CONFLICT (cnpj) DO UPDATE
                      SET payload = EXCLUDED.payload,
                          coletado_em = NOW(),
                          proxima_revalidacao = EXCLUDED.proxima_revalidacao
                """, (cnpj, json.dumps(payload, default=str), proxima))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.debug(f"cache gravar falhou: {e}")
        return False
