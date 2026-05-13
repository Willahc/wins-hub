#!/usr/bin/env python3
"""V7.2 — recovery decisores das 141 obras tagged DECISOR_DE_EMPRESA_ERRADA_V7.

T1: Domain Search nos 8 dominios validados (6 primary + 2 secondary)
T2: rank candidatos por prioridade cargo (compras > engenharia > gerencia)
T4: UPDATE obras com melhor decisor por CNPJ (substitui decisor errado)
"""
from __future__ import annotations
import atexit, json, logging, os, sys, time, re
sys.path.insert(0, "/app")
import psycopg2, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v7_2")

DB = dict(host="db", port=5432, dbname="wins_hub", user="postgres",
          password=os.environ["DB_PASSWORD"])
HUNTER = os.environ["HUNTER_API_KEY"]

# (cnpj, empresa, dominio, tier)
DOMINIOS_BUSCA = [
    ("33592510000154", "Vale S.A.", "vale.com", "primary"),
    ("02359572000430", "Anglo American Minério Ferro", "brasil.angloamerican.com", "primary"),
    ("02359572000430", "Anglo American Minério Ferro", "angloamerican.com", "secondary"),
    ("03983431000103", "EDP Energias Brasil", "edpbr.com.br", "primary"),
    ("03983431000103", "EDP Energias Brasil", "edp.com.br", "secondary"),
    ("16628281000161", "Samarco", "samarco.com", "primary"),
    ("42278291000124", "Log-In Logística", "loginlogistica.com.br", "primary"),
    ("04892707000100", "CCR RioSP", "grupoccr.com.br", "primary"),
]

STATS = {
    "t1_searches": 0, "t1_contatos_inseridos": 0, "t1_validos": 0,
    "t2_candidatos_top": 0,
    "t4_obras_atualizadas_por_cnpj": {},
    "t4_total_atualizadas": 0,
    "tempo_seg": 0,
}
T0 = time.time()


@atexit.register
def _emit():
    STATS["tempo_seg"] = round(time.time() - T0, 1)
    print(f"STATS_JSON: {json.dumps(STATS)}", flush=True)


def domain_search(dominio: str, limit: int = 25):
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search",
                         params={"domain": dominio, "limit": limit, "type": "personal",
                                 "api_key": HUNTER}, timeout=30)
        if r.status_code == 200:
            return r.json().get("data") or None
        log.warning(f"  domain-search HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"  domain-search erro {dominio}: {e}")
    return None


def main():
    conn = psycopg2.connect(**DB)

    # ============ T1: Domain Search ============
    log.info("=" * 60)
    log.info("T1 — Domain Search nos 8 dominios validados")
    log.info("=" * 60)
    for cnpj, empresa, dominio, tier in DOMINIOS_BUSCA:
        STATS["t1_searches"] += 1
        log.info(f"  [{STATS['t1_searches']}/8] {empresa} @ {dominio} ({tier})")
        data = domain_search(dominio, 25)
        if not data:
            time.sleep(2); continue
        emails = data.get("emails") or []
        log.info(f"    retornou {len(emails)} emails")
        for em in emails:
            email = (em.get("value") or "").strip().lower()
            if not email: continue
            nome_full = " ".join(filter(None, [em.get("first_name"), em.get("last_name")])).strip() or None
            cargo = em.get("position") or None
            depto = em.get("department") or None
            li = em.get("linkedin") or None
            score = int(em.get("confidence") or 0)
            ver = (em.get("verification") or {})
            status = (ver.get("status") or em.get("type") or "").lower() or None
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO contatos_alternativos (
                            cnpj, empresa_dominio, email, nome, cargo, departamento,
                            linkedin_url, hunter_score, hunter_status, origem
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'V7.2_recovery')
                        ON CONFLICT (email) DO UPDATE SET
                            cnpj=COALESCE(contatos_alternativos.cnpj, EXCLUDED.cnpj),
                            empresa_dominio=COALESCE(contatos_alternativos.empresa_dominio, EXCLUDED.empresa_dominio),
                            nome=COALESCE(contatos_alternativos.nome, EXCLUDED.nome),
                            cargo=COALESCE(contatos_alternativos.cargo, EXCLUDED.cargo),
                            departamento=COALESCE(contatos_alternativos.departamento, EXCLUDED.departamento),
                            hunter_score=GREATEST(contatos_alternativos.hunter_score, EXCLUDED.hunter_score),
                            hunter_status=COALESCE(contatos_alternativos.hunter_status, EXCLUDED.hunter_status),
                            origem=CASE WHEN contatos_alternativos.origem IS NULL THEN EXCLUDED.origem
                                        ELSE contatos_alternativos.origem || '+V7.2' END
                        RETURNING id
                    """, (cnpj, dominio, email, nome_full, cargo, depto, li, score, status))
                    inserted = cur.fetchone()
                conn.commit()
                if inserted:
                    STATS["t1_contatos_inseridos"] += 1
                    if status and status.lower() in ("valid", "accept_all"):
                        STATS["t1_validos"] += 1
            except Exception as e:
                log.warning(f"  insert erro {email}: {e}")
                conn.rollback()
        time.sleep(2)

    log.info(f"T1 final: {STATS['t1_contatos_inseridos']} inseridos, {STATS['t1_validos']} valid")

    # ============ T2: rank candidatos ============
    log.info("=" * 60)
    log.info("T2 — Rank candidatos por prioridade de cargo")
    log.info("=" * 60)
    with conn.cursor() as cur:
        cur.execute("""
            WITH ranked AS (
              SELECT cnpj, empresa_dominio, email, nome, cargo, departamento,
                     linkedin_url, hunter_score, hunter_status,
                     CASE
                       WHEN LOWER(cargo) LIKE '%%suprimento%%' OR LOWER(cargo) LIKE '%%compra%%'
                         OR LOWER(cargo) LIKE '%%procurement%%' OR LOWER(cargo) LIKE '%%sourcing%%' THEN 1
                       WHEN LOWER(cargo) LIKE '%%engenh%%' OR LOWER(cargo) LIKE '%%projeto%%'
                         OR LOWER(cargo) LIKE '%%obras%%' OR LOWER(cargo) LIKE '%%manuten%%'
                         OR LOWER(cargo) LIKE '%%industrial%%' THEN 2
                       WHEN LOWER(cargo) LIKE '%%gerente%%' OR LOWER(cargo) LIKE '%%coordenador%%'
                         OR LOWER(cargo) LIKE '%%head%%' OR LOWER(cargo) LIKE '%%superintend%%' THEN 3
                       ELSE 99
                     END AS prioridade
              FROM contatos_alternativos
              WHERE origem LIKE '%%V7.2_recovery%%' OR origem LIKE '%%V7.2%%'
                AND cnpj IN (
                  '33592510000154','02359572000430','03983431000103',
                  '16628281000161','42278291000124','04892707000100'
                )
                AND LOWER(COALESCE(hunter_status,'')) IN ('valid','accept_all')
                AND COALESCE(hunter_score,0) >= 70
                AND cargo IS NOT NULL AND TRIM(cargo) != ''
                AND LOWER(cargo) NOT LIKE '%%presidente%%'
                AND LOWER(cargo) NOT LIKE '%%ceo%%'
                AND LOWER(cargo) NOT LIKE '%% vp %%'
                AND LOWER(cargo) NOT LIKE '%%vice%%'
                AND LOWER(cargo) NOT LIKE '%%founder%%'
                AND LOWER(cargo) NOT LIKE '%%marketing%%'
                AND LOWER(cargo) NOT LIKE '%%comercial%%'
                AND LOWER(cargo) NOT LIKE '%%vendas%%'
                AND LOWER(cargo) NOT LIKE '%%recursos humanos%%'
                AND LOWER(cargo) NOT LIKE '%%comunic%%'
                AND LOWER(cargo) NOT LIKE '%%juridic%%'
                AND LOWER(cargo) NOT LIKE '%%legal%%'
                AND LOWER(cargo) NOT LIKE '%%financ%%'
            ),
            with_rank AS (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY cnpj ORDER BY prioridade, hunter_score DESC
              ) AS rk
              FROM ranked WHERE prioridade < 99
            )
            SELECT cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status
            FROM with_rank WHERE rk=1
            ORDER BY cnpj
        """)
        best = cur.fetchall()
    STATS["t2_candidatos_top"] = len(best)
    log.info(f"T2 selecionados: {len(best)} (1 por CNPJ)")
    for cnpj, dom, email, nome, cargo, sc, status in best:
        log.info(f"  {cnpj}: {nome} | {cargo} | {email} | sc={sc} {status}")

    if not best:
        log.warning("T2 retornou ZERO candidatos — fim sem updates")
        conn.close()
        return 0

    # ============ T4: UPDATE obras ============
    log.info("=" * 60)
    log.info("T4 — UPDATE obras tagged com decisores recuperados")
    log.info("=" * 60)
    for cnpj, dom, email, nome, cargo, sc, status in best:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE obras SET
                        nivel1_nome = %s,
                        nivel1_cargo = %s,
                        nivel1_email = %s,
                        nivel1_email_smtp_verified = true,
                        nivel1_email_score = %s,
                        nivel1_email_status = %s,
                        nivel1_email_verified_at = NOW(),
                        decisor_status = 'RECOVERED_V7.2',
                        nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || '+V7.2_recovery',
                        nivel1_enrichment_data = NOW()
                    WHERE cnpj = %s
                      AND decisor_status = 'DECISOR_DE_EMPRESA_ERRADA_V7'
                """, (nome, cargo, email, sc, status, cnpj))
                affected = cur.rowcount
            conn.commit()
            STATS["t4_obras_atualizadas_por_cnpj"][cnpj] = affected
            STATS["t4_total_atualizadas"] += affected
            log.info(f"  ✓ cnpj={cnpj} → {affected} obras atualizadas com {nome} ({email})")
        except Exception as e:
            log.warning(f"  update {cnpj}: {e}"); conn.rollback()
    conn.close()
    return 0


if __name__ == "__main__":
    try: rc = main()
    except BaseException as e:
        import traceback; log.error(f"UNCAUGHT: {e}"); log.error(traceback.format_exc()); rc = 1
    os._exit(rc or 0)
