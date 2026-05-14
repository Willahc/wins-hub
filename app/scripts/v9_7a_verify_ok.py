"""V9.7a — Email Verifier nos 4 PRATA com email no dominio correto mas smtp_verified=false."""
import os, time, requests, psycopg2

HUNTER_KEY = os.environ['HUNTER_API_KEY']

EMAILS = [
    ('16404287000155', 'mario.souza@suzano.com.br'),
    ('07526557000100', 'ricardo.oliveira@ambev.com.br'),
    ('38327308000119', 'claudio.villa@pontesalvadoritaparica.com.br'),
    ('60476884000187', 'renato.bastos@airliquide.com'),
]

conn = psycopg2.connect(
    host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'])
cur = conn.cursor()

for cnpj, email in EMAILS:
    print(f"\n--- {email} ({cnpj}) ---")
    r = requests.get("https://api.hunter.io/v2/email-verifier",
        params={'email': email, 'api_key': HUNTER_KEY}, timeout=30)
    j = r.json()
    if 'errors' in j:
        print(f"  ERR: {j['errors']}")
        continue
    d = j.get('data', {})
    status = d.get('status') or 'unknown'
    score = d.get('score') or 0
    print(f"  status={status} score={score} smtp={d.get('smtp_check')} accept_all={d.get('accept_all')}")
    smtp_ok = status in ('valid','accept_all','webmail')
    cur.execute("""
        UPDATE obras
        SET nivel1_email_smtp_verified=%s,
            nivel1_email_status=%s,
            nivel1_email_score=GREATEST(COALESCE(nivel1_email_score,0), %s)
        WHERE cnpj=%s AND nivel1_email=%s AND classificacao_computed='PRATA'
    """, (smtp_ok, status, score, cnpj, email))
    print(f"  obras: {cur.rowcount} (smtp_ok={smtp_ok})")
    conn.commit()
    time.sleep(1)

cur.close()
conn.close()
