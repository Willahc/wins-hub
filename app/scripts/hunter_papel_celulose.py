"""Hunter pros CEOs/Diretores Suzano/Klabin/Bracell."""
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
    return r.json().get("data") if r.status_code == 200 else None

def hverify(em):
    r = requests.get("https://api.hunter.io/v2/email-verifier",
                     params={"email": em, "api_key": KEY}, timeout=15)
    return r.json().get("data") if r.status_code == 200 else None

ALVOS = [
    # (nome_completo, first, last, domain, empresa_match_pattern, cargo)
    # Suzano — Beto Abreu é CEO desde 2024
    ("Beto Abreu",       "Beto","Abreu",  "suzano.com.br", "Suzano", "CEO Suzano (Diretor-Presidente)"),
    ("Walter Schalka",   "Walter","Schalka","suzano.com.br","Suzano","Conselho Suzano (ex-CEO)"),
    # Klabin
    ("Cristiano Teixeira","Cristiano","Teixeira","klabin.com.br","Klabin","CEO Klabin (Diretor-Presidente)"),
    # Bracell
    ("Praveen Singhavi", "Praveen","Singhavi","bracell.com",  "Bracell","CEO Bracell"),
    ("Pratim Biswas",    "Pratim","Biswas",   "bracell.com",  "Bracell","Executive Bracell"),
]

def main():
    conn = psycopg2.connect(**DB); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    for nome_full, fn, ln, dom, emp_match, cargo in ALVOS:
        print(f"\n== {nome_full} @ {dom} ==")
        d = hfind(fn, ln, dom)
        em = d.get("email") if d else None
        sc = d.get("score") if d else None
        if not em or (sc or 0) < 50:
            print(f"  skip (email={em} score={sc})")
            continue
        v = hverify(em); st = (v or {}).get("status"); smtp_ok = st=="valid"
        print(f"  email={em} score={sc} verify={st}")

        # Inserir em todas obras visíveis OURO/PRATA da empresa
        cur.execute("""
            SELECT id FROM obras
             WHERE empresa ILIKE %s
               AND visivel=true
               AND classificacao_computed IN ('OURO','PRATA')
        """, (f"%{emp_match}%",))
        obras = [r["id"] for r in cur.fetchall()]
        print(f"  obras alvo: {len(obras)}")
        inserts = 0
        for oid in obras:
            cur.execute("""
                INSERT INTO decisores_obra
                  (obra_id, nome, cargo, email, fonte, registrado_por, confianca_match, observacoes)
                VALUES (%s, %s, %s, %s,
                        'hunter_email_finder_papel_20260528','manual:hunter_papel_20260528', 95,
                        'Hunter score=' || %s || ' verify=' || COALESCE(%s,'?'))
                ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
            """, (oid, nome_full, cargo, em, sc, st))
            if cur.rowcount: inserts += 1
        print(f"  +{inserts} decisores")

        # Atualiza nivel1_ onde está NULL
        cur.execute("""
            UPDATE obras
               SET nivel1_nome=%s, nivel1_email=%s,
                   nivel1_email_score=%s, nivel1_email_status=%s,
                   nivel1_email_smtp_verified=%s, nivel1_email_verified_at=NOW(),
                   nivel1_origem_enrichment='hunter_email_finder_papel_20260528',
                   nivel1_enrichment_data=NOW()
             WHERE empresa ILIKE %s AND visivel=true
               AND classificacao_computed IN ('OURO','PRATA')
               AND (nivel1_email IS NULL OR nivel1_email='')
        """, (nome_full, em, sc, st, smtp_ok, f"%{emp_match}%"))
        print(f"  nivel1_ atualizado: {cur.rowcount}")
        time.sleep(0.5)

    conn.commit()


if __name__ == "__main__":
    main()
