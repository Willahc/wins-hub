#!/usr/bin/env python3
import os, sys, requests, json, time
sys.path.insert(0, '/app')
import psycopg2

DB = dict(host='db', port=5432, dbname='wins_hub', user='postgres',
          password=os.environ['DB_PASSWORD'])
HUNTER = os.environ['HUNTER_API_KEY']

candidatos = [
    ('11ecae1f-3500-42a8-b7cf-266e9c1f1b12', '48725405000113',
     'Felipe Cavalcanti', 'aenabrasil.com.br', None),
    ('3c506b32-dc9c-4312-bedb-85c5dff9a2e8', '35593905000105',
     'Waldir Junior', 'ecovias.com.br', 'Waldir.Junior@ecovias.com.br'),
]

stats = {'tentados': 0, 'novos': 0, 'revalidados': 0, 'falharam': 0}
conn = psycopg2.connect(**DB)

for obra_id, cnpj, nome, dominio, email_atual in candidatos:
    stats['tentados'] += 1
    print(f"\n--- {cnpj} {nome} @ {dominio} (atual: {email_atual})")
    r = requests.get('https://api.hunter.io/v2/email-finder',
                     params={'domain': dominio, 'full_name': nome, 'api_key': HUNTER},
                     timeout=20)
    if r.status_code != 200:
        print(f"  finder HTTP {r.status_code}"); stats['falharam'] += 1; continue
    d = (r.json() or {}).get('data') or {}
    em = (d.get('email') or '').lower().strip()
    sc = int(d.get('score') or 0)
    print(f"  Finder -> {em or 'NULL'} score={sc}")
    if not em or sc < 50:
        stats['falharam'] += 1
        time.sleep(0.6); continue
    rv = requests.get('https://api.hunter.io/v2/email-verifier',
                      params={'email': em, 'api_key': HUNTER}, timeout=20)
    if rv.status_code in (200, 222):
        v = (rv.json() or {}).get('data') or {}
        status = (v.get('status') or v.get('result') or 'unknown').lower()
        v_sc = int(v.get('score') or sc)
    else:
        status, v_sc = 'unverified', sc
    valido = status in ('valid', 'accept_all') and v_sc >= 60
    print(f"  Verifier -> {status} score={v_sc} valido={valido}")
    if not valido:
        stats['falharam'] += 1
        time.sleep(0.6); continue

    em_atual_norm = (email_atual or '').lower().strip()
    if em == em_atual_norm:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE obras SET nivel1_email_smtp_verified=true,
                  nivel1_email_status=%s, nivel1_email_score=%s,
                  nivel1_email_verified_at=NOW(),
                  nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+V7_revalidado'
                WHERE id=%s
            """, (status, v_sc, obra_id))
        conn.commit()
        stats['revalidados'] += 1
        print(f"  OK REVALIDADO")
    else:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE obras SET nivel1_email=%s, nivel1_email_score=%s,
                  nivel1_email_smtp_verified=true, nivel1_email_status=%s,
                  nivel1_email_verified_at=NOW(),
                  nivel1_origem_enrichment=COALESCE(nivel1_origem_enrichment,'')||'+V7_finder',
                  nivel1_enrichment_data=NOW()
                WHERE id=%s
            """, (em, v_sc, status, obra_id))
        conn.commit()
        stats['novos'] += 1
        print(f"  OK NOVO email")
    time.sleep(0.6)

conn.close()
print(f"\nSTATS: {json.dumps(stats)}")
