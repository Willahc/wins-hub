#!/usr/bin/env python3
"""T4: Hunter Email Finder + Verifier nas obras recém-CNPJadas."""
import os
import sys
import time
import json
import re
import urllib.request
import urllib.parse
import psycopg2

UA = "wins-hub-enrichment/8.2 (+williamvnvn@gmail.com)"

def http_get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

API_KEY = os.environ["HUNTER_API_KEY"]
MAX_SEARCHES = 50

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=5432, user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD", "postgres"),
    dbname=os.environ.get("DB_NAME", "wins_hub"),
)
cur = conn.cursor()
cur.execute("""
    SELECT o.id, o.empresa, o.nivel1_nome, o.nivel1_cargo, ed.dominio
    FROM obras o
    JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed IN ('OURO','PRATA')
      AND o.nivel1_nome IS NOT NULL
      AND LENGTH(o.nivel1_nome) > 5
      AND o.nivel1_email IS NULL
      AND ed.dominio IS NOT NULL
      AND o.nivel1_nome NOT IN ('Contato Comercial','RH','Comercial')
      AND LOWER(COALESCE(o.nivel1_cargo,'')) NOT LIKE '%presidente%'
      AND LOWER(COALESCE(o.nivel1_cargo,'')) NOT LIKE '%ceo%'
      AND LOWER(COALESCE(o.nivel1_cargo,'')) NOT LIKE '%diretor-presidente%'
    ORDER BY o.valor_estimado DESC NULLS LAST
""")
rows = cur.fetchall()

EXCLUDE_PATTERNS = re.compile(r"(S\.?A\.?|LTDA|S/A|S\.A|EIRELI|ME\b|EPP\b|EMPRESA|CONSTRUTORA|S\.A,|consórcio|ECB)", re.IGNORECASE)

def is_company_like(name: str) -> bool:
    return bool(EXCLUDE_PATTERNS.search(name)) or "(" in name

def split_name(full: str):
    parts = re.sub(r"[—\-].*", "", full).strip().split()
    if len(parts) < 2:
        return None, None
    return parts[0], parts[-1]

candidates = []
seen = set()
for oid, empresa, nome, cargo, dominio in rows:
    if is_company_like(nome):
        continue
    fn, ln = split_name(nome)
    if not fn or not ln:
        continue
    key = (fn.lower(), ln.lower(), dominio)
    if key in seen:
        continue
    seen.add(key)
    candidates.append((oid, empresa, nome, cargo, dominio, fn, ln))

print(f"[T4] {len(rows)} candidates raw -> {len(candidates)} unique person-like", flush=True)
if len(candidates) > MAX_SEARCHES:
    candidates = candidates[:MAX_SEARCHES]
    print(f"[T4] capped to {MAX_SEARCHES}", flush=True)

emails_found = 0
emails_valid = 0
no_email = 0
errors = 0

person_cache = {}

for i, (oid, empresa, nome, cargo, dominio, fn, ln) in enumerate(candidates, 1):
    key = (fn.lower(), ln.lower(), dominio)
    if key in person_cache:
        finder_result = person_cache[key]
        print(f"[{i}/{len(candidates)}] CACHE {fn} {ln}@{dominio}", flush=True)
    else:
        url = "https://api.hunter.io/v2/email-finder?" + urllib.parse.urlencode({
            "domain": dominio,
            "first_name": fn,
            "last_name": ln,
            "api_key": API_KEY,
        })
        try:
            data = http_get_json(url)
            finder_result = data.get("data", {})
            person_cache[key] = finder_result
            time.sleep(2)
        except Exception as e:
            errors += 1
            print(f"[{i}/{len(candidates)}] FINDER ERROR {fn} {ln}@{dominio}: {e}", flush=True)
            continue

    email = finder_result.get("email")
    score = finder_result.get("score")

    if not email:
        no_email += 1
        print(f"[{i}/{len(candidates)}] NO EMAIL: {fn} {ln}@{dominio} (score={score})", flush=True)
        continue

    vurl = "https://api.hunter.io/v2/email-verifier?" + urllib.parse.urlencode({
        "email": email, "api_key": API_KEY
    })
    try:
        vdata = http_get_json(vurl)
        vresult = vdata.get("data", {}).get("result", "")
        vscore = vdata.get("data", {}).get("score")
        smtp_ok = vresult in ("deliverable", "valid", "accept_all", "webmail")
        is_valid = vresult in ("deliverable", "valid")
        cur.execute("""
            UPDATE obras SET
              nivel1_email = %s,
              nivel1_email_smtp_verified = %s,
              nivel1_email_status = %s,
              nivel1_email_score = %s,
              nivel1_email_verified_at = NOW(),
              nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || ' +hunter_finder_v8_2'
            WHERE id = %s
        """, (email, smtp_ok, vresult, vscore, oid))
        conn.commit()
        emails_found += 1
        if is_valid:
            emails_valid += 1
        print(f"[{i}/{len(candidates)}] FOUND: {email} | {vresult} | finder_score={score} verifier_score={vscore}", flush=True)
        time.sleep(1)
    except Exception as e:
        errors += 1
        print(f"[{i}/{len(candidates)}] VERIFIER ERROR {email}: {e}", flush=True)

print(f"\n=== T4 RESULT === found={emails_found} (valid={emails_valid}) no_email={no_email} errors={errors}")
cur.close()
conn.close()
