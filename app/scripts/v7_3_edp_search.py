#!/usr/bin/env python3
"""V7.3 EDP — Domain Search em edp.com.br + energiasdobrasil.com.br."""
from __future__ import annotations
import os, sys, time, json, requests
sys.path.insert(0, "/app")
import psycopg2

DB = dict(host="db", port=5432, dbname="wins_hub", user="postgres",
          password=os.environ["DB_PASSWORD"])
HUNTER = os.environ["HUNTER_API_KEY"]

DOMINIOS = [
    ("03983431000103", "EDP Energias Brasil", "edp.com.br"),
    ("03983431000103", "EDP Energias Brasil", "energiasdobrasil.com.br"),
]

def domain_search(dominio):
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search",
                         params={"domain": dominio, "limit": 25, "type": "personal",
                                 "api_key": HUNTER}, timeout=30)
        if r.status_code == 200:
            return r.json().get("data") or None
        print(f"  HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"  erro {dominio}: {e}", flush=True)
    return None

def main():
    conn = psycopg2.connect(**DB)
    stats = {"searches": 0, "contatos_inseridos": 0, "validos": 0}
    for cnpj, empresa, dom in DOMINIOS:
        stats["searches"] += 1
        print(f"\n=== {empresa} @ {dom} ===", flush=True)
        d = domain_search(dom)
        if not d:
            time.sleep(2); continue
        emails = d.get("emails") or []
        print(f"  retornou {len(emails)} emails", flush=True)
        for em in emails:
            email = (em.get("value") or "").strip().lower()
            if not email: continue
            nome = " ".join(filter(None, [em.get("first_name"), em.get("last_name")])).strip() or None
            cargo = em.get("position") or None
            depto = em.get("department") or None
            li = em.get("linkedin") or None
            score = int(em.get("confidence") or 0)
            status = ((em.get("verification") or {}).get("status") or em.get("type") or "").lower() or None
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO contatos_alternativos (cnpj, empresa_dominio, email, nome,
                          cargo, departamento, linkedin_url, hunter_score, hunter_status, origem)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'V7.3_edp_recovery')
                        ON CONFLICT (email) DO UPDATE SET
                          empresa_dominio=COALESCE(contatos_alternativos.empresa_dominio, EXCLUDED.empresa_dominio),
                          cargo=COALESCE(contatos_alternativos.cargo, EXCLUDED.cargo),
                          hunter_score=GREATEST(contatos_alternativos.hunter_score, EXCLUDED.hunter_score),
                          hunter_status=COALESCE(contatos_alternativos.hunter_status, EXCLUDED.hunter_status)
                        RETURNING id
                    """, (cnpj, dom, email, nome, cargo, depto, li, score, status))
                    inserted = cur.fetchone()
                conn.commit()
                if inserted:
                    stats["contatos_inseridos"] += 1
                    if status in ("valid","accept_all"):
                        stats["validos"] += 1
            except Exception as e:
                print(f"  insert {email}: {e}", flush=True); conn.rollback()
        time.sleep(2)
    conn.close()
    print(f"\nSTATS: {json.dumps(stats)}", flush=True)

if __name__ == "__main__":
    try: main()
    except Exception as e:
        import traceback; traceback.print_exc()
