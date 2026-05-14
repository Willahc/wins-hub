#!/usr/bin/env python3
"""T5 V9 — Hunter Email Finder em obras com nivel1_nome mas sem email."""
import os
import re
import time
import json
import urllib.request
import urllib.parse
import psycopg2

API_KEY = os.environ["HUNTER_API_KEY"]
UA = "wins-hub-enrichment/9.0 (+williamvnvn@gmail.com)"
MAX = 60
SLEEP_BETWEEN = 1.5

EXCLUDE_NAME = re.compile(r"(S\.?A\.?|LTDA|S/A|S\.A|EIRELI|EPP\b|ME\b|EMPRESA|CONSTRUTORA|CONSORCIO|CONSÓRCIO|ECB)", re.I)

def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())

def split_name(full):
    parts = re.sub(r"[—\-].*", "", full).strip().split()
    if len(parts) < 2:
        return None, None
    return parts[0], parts[-1]

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=5432,
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD"),
    dbname=os.environ.get("DB_NAME", "wins_hub"),
)
cur = conn.cursor()
cur.execute("""
    SELECT o.id, o.empresa, o.nivel1_nome, o.nivel1_cargo, ed.dominio
    FROM obras o
    JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed IN ('OURO','PRATA')
      AND o.nivel1_nome IS NOT NULL AND LENGTH(o.nivel1_nome) > 5
      AND (o.nivel1_email IS NULL OR o.nivel1_email = '')
      AND ed.dominio IS NOT NULL
      AND o.nivel1_nome NOT IN ('Contato Comercial','RH','Comercial')
      AND LOWER(COALESCE(o.nivel1_cargo,'')) NOT LIKE '%%presidente%%'
      AND LOWER(COALESCE(o.nivel1_cargo,'')) NOT LIKE '%%ceo%%'
    ORDER BY o.valor_estimado DESC NULLS LAST
""")
rows = cur.fetchall()
print(f"[T5] {len(rows)} raw candidates", flush=True)

deduped = []
seen = set()
for oid, empresa, nome, cargo, dom in rows:
    if EXCLUDE_NAME.search(nome) or "(" in nome:
        continue
    fn, ln = split_name(nome)
    if not fn or not ln:
        continue
    key = (fn.lower(), ln.lower(), dom)
    if key in seen:
        continue
    seen.add(key)
    deduped.append((oid, empresa, nome, cargo, dom, fn, ln))

print(f"[T5] {len(deduped)} unique person-like", flush=True)
if len(deduped) > MAX:
    deduped = deduped[:MAX]

cache = {}
found = 0
valid = 0
no_email = 0
errors = 0
low_score_skip = 0

for i, (oid, empresa, nome, cargo, dom, fn, ln) in enumerate(deduped, 1):
    key = (fn.lower(), ln.lower(), dom)
    if key in cache:
        f_data = cache[key]
        print(f"[{i}/{len(deduped)}] CACHE {fn} {ln} @ {dom}", flush=True)
    else:
        url = "https://api.hunter.io/v2/email-finder?" + urllib.parse.urlencode({
            "domain": dom, "first_name": fn, "last_name": ln, "api_key": API_KEY
        })
        try:
            d = http_get_json(url)
            f_data = d.get("data", {})
            cache[key] = f_data
            time.sleep(SLEEP_BETWEEN)
        except Exception as e:
            errors += 1
            print(f"[{i}/{len(deduped)}] FIND ERR {fn} {ln}@{dom}: {e}", flush=True)
            continue

    email = f_data.get("email")
    score = f_data.get("score") or 0
    if not email:
        no_email += 1
        print(f"[{i}/{len(deduped)}] NO_EMAIL {fn} {ln}@{dom}", flush=True)
        continue
    if score < 50:
        low_score_skip += 1
        print(f"[{i}/{len(deduped)}] LOW_SCORE {email} ({score})", flush=True)
        continue

    vurl = "https://api.hunter.io/v2/email-verifier?" + urllib.parse.urlencode({
        "email": email, "api_key": API_KEY
    })
    try:
        v = http_get_json(vurl)
    except Exception as e:
        errors += 1
        print(f"[{i}/{len(deduped)}] VER ERR {email}: {e}", flush=True)
        time.sleep(SLEEP_BETWEEN)
        continue

    vresult = v.get("data", {}).get("result", "")
    vscore = v.get("data", {}).get("score")
    smtp_ok = vresult in ("deliverable", "valid", "accept_all", "webmail")
    is_valid = vresult in ("deliverable", "valid")

    cur.execute("""
        UPDATE obras SET
          nivel1_email = %s,
          nivel1_email_smtp_verified = %s,
          nivel1_email_status = %s,
          nivel1_email_score = %s,
          nivel1_email_verified_at = NOW(),
          nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || ' +V9_finder'
        WHERE id = %s
    """, (email, smtp_ok, vresult, vscore, oid))
    conn.commit()
    found += 1
    if is_valid:
        valid += 1
    print(f"[{i}/{len(deduped)}] FOUND {email} | {vresult} | finder={score} verifier={vscore}", flush=True)
    time.sleep(SLEEP_BETWEEN)

print(f"\n=== T5 RESULT === found={found} valid={valid} no_email={no_email} low_score={low_score_skip} errors={errors}")
cur.close()
conn.close()
