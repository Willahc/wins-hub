import logging
from datetime import datetime, timedelta
from typing import List, Optional
from psycopg2.extras import RealDictCursor

from sales_intelligence.db import get_conn

log = logging.getLogger("sales_intel.cache_decisores")


def buscar_por_cnpj(cnpj: str) -> List:
    """Retorna decisores ativos do cache (revalidacao>hoje, excluido_em IS NULL)."""
    from sales_intelligence.models.decisor import Decisor
    try:
        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT cnpj, nome_pessoa, cargo_raw, cargo_normalizado, tipo_cargo,
                           cargo_idioma, cargo_nivel, confianca, fonte_descoberta,
                           fonte_secundaria, snippet_origem, url_origem, linkedin_slug,
                           email, email_status, score_relevancia, descoberto_em, revalidacao
                      FROM empresa_decisores_cache
                     WHERE cnpj = %s
                       AND excluido_em IS NULL
                       AND (revalidacao IS NULL OR revalidacao > CURRENT_DATE)
                     ORDER BY score_relevancia DESC NULLS LAST
                """, (cnpj,))
                rows = cur.fetchall()
        finally:
            conn.close()
        return [Decisor(
            cnpj=r["cnpj"], nome_pessoa=r["nome_pessoa"], cargo_raw=r["cargo_raw"],
            cargo_normalizado=r["cargo_normalizado"], tipo_cargo=r["tipo_cargo"] or "OUTRO",
            cargo_idioma=r["cargo_idioma"], cargo_nivel=r["cargo_nivel"],
            confianca=r["confianca"], fonte_descoberta=r["fonte_descoberta"],
            fonte_secundaria=r["fonte_secundaria"], snippet_origem=r["snippet_origem"] or "",
            url_origem=r["url_origem"] or "", linkedin_slug=r["linkedin_slug"],
            email=r["email"], email_status=r["email_status"],
            score_relevancia=float(r["score_relevancia"] or 0.0),
            descoberto_em=r["descoberto_em"], revalidacao=r["revalidacao"],
        ) for r in rows]
    except Exception as e:
        log.warning(f"buscar_por_cnpj falhou cnpj={cnpj}: {e}")
        return []


def gravar_decisor(decisor, validade_dias: int = 180) -> bool:
    """UPSERT pelo indice unique funcional (cnpj, lower(immutable_unaccent(nome_pessoa)))."""
    try:
        revalidacao = (datetime.utcnow() + timedelta(days=validade_dias)).date()
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO empresa_decisores_cache (
                        cnpj, nome_pessoa, cargo_raw, cargo_normalizado, tipo_cargo,
                        cargo_idioma, cargo_nivel, confianca, fonte_descoberta,
                        fonte_secundaria, snippet_origem, url_origem, linkedin_slug,
                        email, email_status, score_relevancia, descoberto_em, revalidacao
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                    ON CONFLICT ON CONSTRAINT idx_empresa_decisores_cnpj_pessoa_uniq
                    DO UPDATE SET
                        cargo_raw = EXCLUDED.cargo_raw,
                        cargo_normalizado = EXCLUDED.cargo_normalizado,
                        tipo_cargo = EXCLUDED.tipo_cargo,
                        cargo_idioma = EXCLUDED.cargo_idioma,
                        cargo_nivel = EXCLUDED.cargo_nivel,
                        confianca = EXCLUDED.confianca,
                        fonte_descoberta = EXCLUDED.fonte_descoberta,
                        fonte_secundaria = EXCLUDED.fonte_secundaria,
                        snippet_origem = EXCLUDED.snippet_origem,
                        url_origem = EXCLUDED.url_origem,
                        linkedin_slug = EXCLUDED.linkedin_slug,
                        score_relevancia = EXCLUDED.score_relevancia,
                        revalidacao = EXCLUDED.revalidacao
                """, (
                    decisor.cnpj, decisor.nome_pessoa, decisor.cargo_raw,
                    decisor.cargo_normalizado, decisor.tipo_cargo,
                    decisor.cargo_idioma, decisor.cargo_nivel, decisor.confianca,
                    decisor.fonte_descoberta, decisor.fonte_secundaria,
                    decisor.snippet_origem, decisor.url_origem, decisor.linkedin_slug,
                    decisor.email, decisor.email_status, decisor.score_relevancia,
                    revalidacao,
                ))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        # ON CONFLICT por indice parcial precisa de WHERE no INSERT.
        # Postgres nao suporta ON CONFLICT em indice parcial direto, fazer INSERT
        # com fallback de UPDATE manual.
        log.debug(f"gravar_decisor ON CONFLICT falhou ({e}); tentando UPSERT manual")
        try:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id FROM empresa_decisores_cache
                         WHERE cnpj = %s
                           AND lower(immutable_unaccent(nome_pessoa)) = lower(immutable_unaccent(%s))
                           AND excluido_em IS NULL
                    """, (decisor.cnpj, decisor.nome_pessoa))
                    row = cur.fetchone()
                    revalidacao = (datetime.utcnow() + timedelta(days=validade_dias)).date()
                    if row:
                        cur.execute("""
                            UPDATE empresa_decisores_cache SET
                                cargo_raw=%s, cargo_normalizado=%s, tipo_cargo=%s,
                                cargo_idioma=%s, cargo_nivel=%s, confianca=%s,
                                fonte_descoberta=%s, fonte_secundaria=%s,
                                snippet_origem=%s, url_origem=%s, linkedin_slug=%s,
                                score_relevancia=%s, revalidacao=%s
                             WHERE id=%s
                        """, (decisor.cargo_raw, decisor.cargo_normalizado, decisor.tipo_cargo,
                              decisor.cargo_idioma, decisor.cargo_nivel, decisor.confianca,
                              decisor.fonte_descoberta, decisor.fonte_secundaria,
                              decisor.snippet_origem, decisor.url_origem, decisor.linkedin_slug,
                              decisor.score_relevancia, revalidacao, row[0]))
                    else:
                        cur.execute("""
                            INSERT INTO empresa_decisores_cache (
                                cnpj, nome_pessoa, cargo_raw, cargo_normalizado, tipo_cargo,
                                cargo_idioma, cargo_nivel, confianca, fonte_descoberta,
                                fonte_secundaria, snippet_origem, url_origem, linkedin_slug,
                                email, email_status, score_relevancia, descoberto_em, revalidacao
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                        """, (decisor.cnpj, decisor.nome_pessoa, decisor.cargo_raw,
                              decisor.cargo_normalizado, decisor.tipo_cargo,
                              decisor.cargo_idioma, decisor.cargo_nivel, decisor.confianca,
                              decisor.fonte_descoberta, decisor.fonte_secundaria,
                              decisor.snippet_origem, decisor.url_origem, decisor.linkedin_slug,
                              decisor.email, decisor.email_status, decisor.score_relevancia,
                              revalidacao))
                conn.commit()
            finally:
                conn.close()
            return True
        except Exception as e2:
            log.warning(f"gravar_decisor UPSERT manual falhou: {e2}")
            return False


def soft_delete(cnpj: str, nome_pessoa: str) -> bool:
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE empresa_decisores_cache
                       SET excluido_em = NOW()
                     WHERE cnpj = %s
                       AND lower(immutable_unaccent(nome_pessoa)) = lower(immutable_unaccent(%s))
                       AND excluido_em IS NULL
                """, (cnpj, nome_pessoa))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning(f"soft_delete falhou: {e}")
        return False
