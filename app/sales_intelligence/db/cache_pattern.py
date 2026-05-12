import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("sales_intel.cache_pattern")

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def _conn():
    return psycopg2.connect(**DB)


def buscar_pattern_cache(dominio: str):
    """Retorna EmailPattern do cache se valido, senao None."""
    try:
        from sales_intelligence.models.email_pattern import EmailPattern
        conn = _conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT padrao, confianca, exemplos, amostra_total,
                           COALESCE(pessoas_count, 0) AS pessoas_count, detectado_em
                      FROM empresa_email_pattern_cache
                     WHERE dominio = %s
                       AND (proxima_revalidacao IS NULL OR proxima_revalidacao > CURRENT_DATE)
                """, (dominio,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return EmailPattern(
            padrao=row["padrao"],
            confianca=row["confianca"],
            exemplos=row["exemplos"] if isinstance(row["exemplos"], list) else json.loads(row["exemplos"]),
            amostra_total=row["amostra_total"],
            pessoas_count=row["pessoas_count"],
            dominio_origem=dominio,
            detectado_em=row["detectado_em"],
        )
    except Exception as e:
        log.debug(f"pattern cache buscar falhou dominio={dominio}: {e}")
        return None


def gravar_pattern_cache(pattern, validade_dias: int = 180) -> bool:
    """UPSERT pattern. Validade longa (180 dias) — padroes mudam menos que cadastrais."""
    try:
        if not hasattr(pattern, "padrao"):
            return False
        proxima = (datetime.utcnow() + timedelta(days=validade_dias)).date()
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO empresa_email_pattern_cache
                      (dominio, padrao, confianca, exemplos, amostra_total,
                       pessoas_count, detectado_em, proxima_revalidacao)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, NOW(), %s)
                    ON CONFLICT (dominio) DO UPDATE
                      SET padrao = EXCLUDED.padrao,
                          confianca = EXCLUDED.confianca,
                          exemplos = EXCLUDED.exemplos,
                          amostra_total = EXCLUDED.amostra_total,
                          pessoas_count = EXCLUDED.pessoas_count,
                          detectado_em = NOW(),
                          proxima_revalidacao = EXCLUDED.proxima_revalidacao
                """, (pattern.dominio_origem, pattern.padrao, pattern.confianca,
                      json.dumps(pattern.exemplos), pattern.amostra_total,
                      getattr(pattern, "pessoas_count", 0), proxima))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.debug(f"pattern cache gravar falhou: {e}")
        return False
