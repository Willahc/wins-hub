"""Retry adjustments: Bruno @ prio3.com.br, Vanzella re-find."""
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

# === Bruno Serapião ===
print("== Bruno Serapião — tentar prio3.com.br ==")
for dom in ["prio3.com.br","prio.com","prio-energy.com","prio.com.br"]:
    d = hfind("Bruno","Serapião",dom)
    print(f"  {dom}: email={d.get('email') if d else None} score={d.get('score') if d else None}")
    if d and d.get("email"):
        em = d["email"]; sc = d.get("score") or 0
        v = hverify(em); st = (v or {}).get("status"); smtp_ok = st == "valid"
        print(f"    verify={st} smtp={(v or {}).get('smtp_check')}")
        if sc >= 50:
            conn = psycopg2.connect(**DB); conn.autocommit = False
            cur = conn.cursor()
            cur.execute("""
                SELECT id FROM obras
                 WHERE empresa ILIKE 'PRIO%' AND classificacao_computed='OURO' AND visivel=true
            """)
            obras = [r[0] for r in cur.fetchall()]
            print(f"    inserindo em {len(obras)} obras PRIO OURO")
            for oid in obras:
                cur.execute("""
                    INSERT INTO decisores_obra
                      (obra_id, nome, cargo, email, fonte, registrado_por, confianca_match, observacoes)
                    VALUES (%s,'Bruno Serapião','CEO PRIO S.A. (Diretor-Presidente)',%s,
                            'hunter_email_finder_retry_20260528','manual:hunter_retry_20260528', 95,
                            'Hunter retry 28/05 dom=' || %s || ' score=' || %s || ' verify=' || COALESCE(%s,'?'))
                    ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
                """, (oid, em, dom, sc, st))
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
            conn.commit()
            print("    OK commit")
            break
    time.sleep(0.4)

# === Vanzella re-find ===
print("\n== Vinícius Vanzella — re-find Gelprime ==")
for dom, fn, ln in [("gelprime.com.br","Vinicius","Vanzella"),
                     ("gelprime.com.br","Vinicius","Vanzella de Souza"),
                     ("gelprime.com.br","Vinicius","Souza"),
                     ("gelprime.com.br","Vinícius","Vanzella")]:
    d = hfind(fn, ln, dom)
    print(f"  {fn} {ln} @ {dom}: email={d.get('email') if d else None} score={d.get('score') if d else None}")
    time.sleep(0.4)
