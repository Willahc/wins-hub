#!/usr/bin/env python3
"""V14 role-based — tenta emails genéricos (contato/comercial/suprimentos) via verifier.

Última cartada pras 8 obras OURO/PRATA sem decisor: tentar role-based emails que
muitas empresas pequenas usam pra recebimento de propostas.
"""
import os
import time
import json
import urllib.request
import psycopg2

API_KEY = os.environ["HUNTER_API_KEY"]
SLEEP = 1.5

ROLES = [
  "contato", "comercial", "suprimentos", "compras", "atendimento",
  "fornecedores", "rh", "engenharia"
]


def http_get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=5432,
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD"),
    dbname=os.environ.get("DB_NAME", "wins_hub"),
)
cur = conn.cursor()
cur.execute("""
    SELECT ed.dominio, ed.cnpj, ed.empresa_nome
    FROM obras o
    JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed IN ('OURO','PRATA')
      AND (o.nivel1_nome IS NULL OR o.nivel1_nome = '')
      AND (o.visivel IS NULL OR o.visivel = true)
      AND ed.dominio IS NOT NULL
    GROUP BY ed.dominio, ed.cnpj, ed.empresa_nome
""")
domains = cur.fetchall()
print(f"[V14] {len(domains)} domains × {len(ROLES)} roles to try", flush=True)

with_decisor = obras_updated = verifications_used = 0

for dominio, cnpj, empresa in domains:
    found = None
    for role in ROLES:
        email = f"{role}@{dominio}"
        try:
            vr = http_get_json(f"https://api.hunter.io/v2/email-verifier?email={email}&api_key={API_KEY}")
            verifications_used += 1
        except Exception as e:
            print(f"  {email} verify ERR: {e}", flush=True)
            time.sleep(SLEEP)
            continue
        vd = vr.get("data", {}) or {}
        vstatus = vd.get("status") or "?"
        vscore = vd.get("score") or 0
        if vstatus in ("valid", "accept_all", "webmail") and vscore >= 50:
            found = (role, email, vscore, vstatus)
            break
        time.sleep(SLEEP)

    if not found:
        print(f"[{dominio}] ({empresa}): NONE of {len(ROLES)} roles verified", flush=True)
        continue

    role, email, vscore, vstatus = found
    nome = role.title()
    cur.execute("""
        UPDATE obras SET
            nivel1_nome = %s,
            nivel1_cargo = %s,
            nivel1_email = %s,
            nivel1_email_smtp_verified = TRUE,
            nivel1_email_status = %s,
            nivel1_email_score = %s,
            nivel1_email_verified_at = NOW(),
            decisor_status = 'DISCOVERED_V14',
            nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || ' +V14_role'
        WHERE cnpj = %s
          AND classificacao_computed IN ('OURO','PRATA')
          AND (nivel1_nome IS NULL OR nivel1_nome = '')
    """, (nome, f"Canal {role} (role-based)", email, vstatus, vscore, cnpj))
    obras_updated += cur.rowcount
    with_decisor += 1
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V14_role')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dominio, email, nome, f"Canal {role}", vscore, vstatus))
    conn.commit()
    print(f"[{dominio}]: {email} | {vstatus} | score={vscore}", flush=True)

print(f"\n=== V14 RESULT === domains={len(domains)} with_decisor={with_decisor} obras_updated={obras_updated} verifications_used={verifications_used}")
cur.close()
conn.close()
