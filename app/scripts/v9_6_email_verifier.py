"""V9.6.b — Email Verifier nos 5 picks V9.6 (smtp=unknown -> tenta valid)."""
import os, time, requests, psycopg2

HUNTER_KEY = os.environ['HUNTER_API_KEY']

conn = psycopg2.connect(
    host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'])
cur = conn.cursor()

cur.execute("""
    SELECT DISTINCT cnpj, nivel1_email, nivel1_nome
    FROM obras
    WHERE classificacao_computed='PRATA'
      AND nivel1_origem_enrichment LIKE 'V9_6%'
      AND nivel1_email IS NOT NULL
    ORDER BY cnpj
""")
picks = cur.fetchall()
print(f"Picks V9.6 distintos: {len(picks)}")

stats = {'verified': 0, 'valid': 0, 'invalid': 0, 'accept_all': 0, 'risky': 0, 'unknown': 0}

for cnpj, email, nome in picks:
    print(f"\n--- {nome} ({email}) ---")
    try:
        r = requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email': email, 'api_key': HUNTER_KEY}, timeout=30)
        j = r.json()
        if 'errors' in j:
            print(f"  ERR: {j['errors']}")
            continue
        d = j.get('data', {})
        status = d.get('status') or d.get('result') or 'unknown'
        score = d.get('score') or 0
        print(f"  status={status} score={score} smtp_check={d.get('smtp_check')} mx={d.get('mx_records')} dispoable={d.get('disposable')}")
    except Exception as e:
        print(f"  ERR: {e}")
        continue
    stats['verified'] += 1
    smtp_ok = status == 'valid'
    if status == 'valid':
        stats['valid'] += 1
    elif status == 'invalid':
        stats['invalid'] += 1
    elif status == 'accept_all':
        stats['accept_all'] += 1
        smtp_ok = True  # accept_all geralmente recebe
    elif status == 'webmail':
        smtp_ok = True
    elif status == 'risky':
        stats['risky'] += 1
    else:
        stats['unknown'] += 1

    cur.execute("""
        UPDATE obras
        SET nivel1_email_smtp_verified = %s,
            nivel1_email_status = %s,
            nivel1_email_score = GREATEST(COALESCE(nivel1_email_score,0), %s)
        WHERE cnpj=%s
          AND nivel1_origem_enrichment LIKE 'V9_6%%'
          AND nivel1_email=%s
    """, (smtp_ok, status, score, cnpj, email))
    n = cur.rowcount
    conn.commit()
    print(f"  obras updated: {n} (smtp_verified={smtp_ok})")
    time.sleep(1)

print(f"\n=== Verifier stats ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
cur.close()
conn.close()
