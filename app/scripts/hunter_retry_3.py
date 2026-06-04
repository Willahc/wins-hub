"""Hunter retry pros 3 decisores instáveis ontem (28/05).

- Bruno Serapião (CEO PRIO) — INSERT em obras PRIO OURO
- Carlos Barbery (CEO Whirlpool Brasil) — re-verify
- Vinícius Vanzella (CEO Gelprime) — re-verify
"""
import os, sys, time
sys.path.insert(0, "/app")
import psycopg2, psycopg2.extras, requests

DB = dict(host=os.getenv("DB_HOST","db"), port=int(os.getenv("DB_PORT","5432")),
          dbname=os.getenv("DB_NAME","wins_hub"), user=os.getenv("DB_USER","postgres"),
          password=os.getenv("DB_PASSWORD",""))
KEY = os.getenv("HUNTER_API_KEY","").strip()


def hfind(fn, ln, dom):
    r = requests.get("https://api.hunter.io/v2/email-finder",
                     params={"domain": dom, "first_name": fn, "last_name": ln, "api_key": KEY},
                     timeout=15)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.json().get("data") or {}, None

def hverify(em):
    r = requests.get("https://api.hunter.io/v2/email-verifier",
                     params={"email": em, "api_key": KEY}, timeout=15)
    if r.status_code != 200:
        return None
    return r.json().get("data") or {}


def main():
    conn = psycopg2.connect(**DB); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ====== Bruno Serapião — CEO PRIO ======
    print("== Bruno Serapião (CEO PRIO) ==")
    data, err = hfind("Bruno", "Serapião", "prio.com.br")
    if err:
        print(f"  HTTP {err}")
    elif data and data.get("email"):
        em = data["email"]; sc = data["score"]
        v = hverify(em)
        st = (v or {}).get("status")
        smtp_ok = st == "valid"
        print(f"  email={em} score={sc} verify={st}")
        # INSERT em todas obras PRIO OURO RJ/SP visíveis
        cur.execute("""
            SELECT id FROM obras
             WHERE empresa ILIKE 'PRIO%' AND classificacao_computed='OURO' AND visivel=true
        """)
        obras = [r["id"] for r in cur.fetchall()]
        print(f"  Inserindo em {len(obras)} obras PRIO OURO")
        for oid in obras:
            try:
                cur.execute("""
                    INSERT INTO decisores_obra
                      (obra_id, nome, cargo, email, fonte, registrado_por, confianca_match, observacoes)
                    VALUES (%s, 'Bruno Serapião', 'CEO PRIO S.A. (Diretor-Presidente)', %s,
                            'hunter_email_finder_retry_20260528','manual:hunter_retry_20260528', 95,
                            'Hunter retry 28/05 — score=' || %s || ' verify=' || COALESCE(%s,'?'))
                    ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
                """, (oid, em, sc, st))
                if cur.rowcount:
                    print(f"    + {oid}")
            except psycopg2.errors.UniqueViolation:
                pass
        # nivel1_ em obras sem nivel1
        cur.execute("""
            UPDATE obras
               SET nivel1_nome='Bruno Serapião', nivel1_email=%s,
                   nivel1_email_score=%s, nivel1_email_status=%s,
                   nivel1_email_smtp_verified=%s, nivel1_email_verified_at=NOW(),
                   nivel1_origem_enrichment='hunter_email_finder_retry_20260528',
                   nivel1_enrichment_data=NOW()
             WHERE empresa ILIKE 'PRIO%%' AND classificacao_computed='OURO' AND visivel=true
               AND (nivel1_email IS NULL OR nivel1_email='')
        """, (em, sc, st, smtp_ok))
        print(f"  nivel1_ atualizado em {cur.rowcount} obra(s)")
    else:
        print("  ZERO HIT")

    time.sleep(0.5)

    # ====== Carlos Barbery — Whirlpool ======
    print("\n== Carlos Barbery (Whirlpool Brasil) ==")
    cur.execute("SELECT email FROM decisores_obra WHERE nome ILIKE '%barbery%' AND excluido_em IS NULL LIMIT 1")
    row = cur.fetchone()
    if row and row["email"]:
        v = hverify(row["email"])
        st = (v or {}).get("status"); smtp_ok = st == "valid"
        print(f"  current={row['email']} verify={st} smtp={(v or {}).get('smtp_check')}")
        cur.execute("""
            UPDATE decisores_obra
               SET observacoes = COALESCE(observacoes,'') || E'\nHunter retry 28/05 verify=' || COALESCE(%s,'?')
             WHERE nome ILIKE '%%barbery%%' AND excluido_em IS NULL
        """, (st,))
        # propaga p/ obra
        cur.execute("""
            UPDATE obras o
               SET nivel1_email_status=%s, nivel1_email_smtp_verified=%s, nivel1_email_verified_at=NOW()
             WHERE EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id=o.id AND d.nome ILIKE '%%barbery%%' AND d.excluido_em IS NULL)
               AND COALESCE(o.nivel1_email,'') = %s
        """, (st, smtp_ok, row["email"]))
        print(f"  obras atualizadas: {cur.rowcount}")
    else:
        # finder
        data, err = hfind("Carlos","Barbery","whirlpool.com")
        print(f"  finder data={data} err={err}")

    time.sleep(0.5)

    # ====== Vinícius Vanzella — Gelprime ======
    print("\n== Vinícius Vanzella (Gelprime) ==")
    cur.execute("SELECT email FROM decisores_obra WHERE nome ILIKE '%vanzella%' AND excluido_em IS NULL LIMIT 1")
    row = cur.fetchone()
    if row and row["email"]:
        v = hverify(row["email"])
        st = (v or {}).get("status"); smtp_ok = st == "valid"
        print(f"  current={row['email']} verify={st} smtp={(v or {}).get('smtp_check')}")
        cur.execute("""
            UPDATE decisores_obra
               SET observacoes = COALESCE(observacoes,'') || E'\nHunter retry 28/05 verify=' || COALESCE(%s,'?')
             WHERE nome ILIKE '%%vanzella%%' AND excluido_em IS NULL
        """, (st,))
        cur.execute("""
            UPDATE obras o
               SET nivel1_email_status=%s, nivel1_email_smtp_verified=%s, nivel1_email_verified_at=NOW()
             WHERE EXISTS (SELECT 1 FROM decisores_obra d WHERE d.obra_id=o.id AND d.nome ILIKE '%%vanzella%%' AND d.excluido_em IS NULL)
               AND COALESCE(o.nivel1_email,'') = %s
        """, (st, smtp_ok, row["email"]))
        print(f"  obras atualizadas: {cur.rowcount}")
    else:
        data, err = hfind("Vinícius","Vanzella","gelprime.com.br")
        print(f"  finder data={data} err={err}")

    conn.commit()


if __name__ == "__main__":
    main()
