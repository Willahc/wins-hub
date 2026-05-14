#!/usr/bin/env python3
"""T3: Hunter Email Verifier nos OURO indefinidos."""
import os
import sys
import time
import json
import urllib.request
import urllib.parse
import psycopg2

API_KEY = os.environ["HUNTER_API_KEY"]

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=5432, user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD", "postgres"),
    dbname=os.environ.get("DB_NAME", "wins_hub"),
)
cur = conn.cursor()
cur.execute("""
    SELECT id, nivel1_email FROM obras
    WHERE classificacao_computed='OURO'
      AND nivel1_email IS NOT NULL
      AND nivel1_email_smtp_verified IS NULL
""")
rows = cur.fetchall()
print(f"[T3] {len(rows)} obras to verify", flush=True)

verified_valid = 0
verified_invalid = 0
verified_acceptall = 0
errors = 0

for i, (oid, email) in enumerate(rows, 1):
    url = "https://api.hunter.io/v2/email-verifier?" + urllib.parse.urlencode({
        "email": email, "api_key": API_KEY
    })
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read())
        result = data.get("data", {}).get("result", "")
        score = data.get("data", {}).get("score")
        smtp_ok = result in ("deliverable", "valid", "accept_all", "webmail")
        is_valid = result in ("deliverable", "valid")
        cur.execute("""
            UPDATE obras SET
              nivel1_email_smtp_verified = %s,
              nivel1_email_status = %s,
              nivel1_email_score = %s,
              nivel1_email_verified_at = NOW()
            WHERE id = %s
        """, (smtp_ok, result, score, oid))
        conn.commit()
        if is_valid:
            verified_valid += 1
        elif result == "accept_all":
            verified_acceptall += 1
        else:
            verified_invalid += 1
        print(f"[{i}/{len(rows)}] {email}: {result} (score={score})", flush=True)
    except Exception as e:
        errors += 1
        print(f"[{i}/{len(rows)}] {email}: ERROR {e}", flush=True)
    time.sleep(1)

print(f"\n=== T3 RESULT === valid={verified_valid} accept_all={verified_acceptall} invalid={verified_invalid} errors={errors}")
cur.close()
conn.close()
