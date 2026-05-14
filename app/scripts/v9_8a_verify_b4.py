"""V9.8a — Re-verify todos os emails OURO bucket 4 via Email Verifier dedicado."""
import os, time, requests, psycopg2

HUNTER_KEY=os.environ['HUNTER_API_KEY']

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

cur.execute("""
    SELECT DISTINCT cnpj, nivel1_email
    FROM obras
    WHERE classificacao_computed='OURO'
      AND nivel1_email_smtp_verified IS NOT TRUE
      AND nivel1_email IS NOT NULL AND nivel1_email <> ''
    ORDER BY nivel1_email
""")
targets=cur.fetchall()
print(f"Bucket 4 distinct emails: {len(targets)}")

stats={'verified':0,'valid_flips':0,'still_invalid':0,'obras_upd':0}

for cnpj, email in targets:
    d=verify(email); stats['verified']+=1
    status=d.get('status') or 'unknown'
    score=d.get('score') or 0
    smtp_ok = status in ('valid','accept_all','webmail') and score>=50
    flag='✓' if smtp_ok else '✗'
    print(f"  {flag} {email} → {status} {score}")
    if smtp_ok: stats['valid_flips']+=1
    else: stats['still_invalid']+=1
    cur.execute("""
        UPDATE obras
        SET nivel1_email_smtp_verified=%s,
            nivel1_email_status=%s,
            nivel1_email_score=GREATEST(COALESCE(nivel1_email_score,0), %s)
        WHERE classificacao_computed='OURO' AND cnpj=%s AND nivel1_email=%s
    """, (smtp_ok, status, score, cnpj, email))
    if smtp_ok: stats['obras_upd']+=cur.rowcount
    conn.commit()
    time.sleep(0.7)

print(f"\n=== STATS V9.8a ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
