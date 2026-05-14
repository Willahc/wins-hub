"""V9.8c — Email Finder OURO bucket 3 (decisor com nome sem email)."""
import os, time, requests, psycopg2
from unicodedata import normalize

HUNTER_KEY=os.environ['HUNTER_API_KEY']
ROLES=['compras','suprimentos','procurement','engenharia','projetos','manutencao',
       'operacoes','obras','contato','sourcing']

def deaccent(s): return normalize('NFKD',s).encode('ascii','ignore').decode('ascii')
def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}
def finder(domain, first, last):
    try:
        r=requests.get("https://api.hunter.io/v2/email-finder",
            params={'domain':domain,'first_name':first,'last_name':last,
                    'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}
def find_role(domain):
    for role in ROLES:
        e=f"{role}@{domain}"
        d=verify(e); st=d.get('status') or 'unknown'; sc=d.get('score') or 0
        time.sleep(0.5)
        if st in ('valid','accept_all','webmail') and sc>=50:
            return e, role, st, sc
        if st=='unknown' and sc>=70:
            return e, role, 'unknown_accept', sc
    return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

cur.execute("""
    SELECT DISTINCT o.cnpj, ed.dominio, MAX(o.empresa) AS empresa, MAX(o.nivel1_nome) AS nome
    FROM obras o JOIN empresa_dominios ed ON o.cnpj=ed.cnpj
    WHERE o.classificacao_computed='OURO'
      AND o.nivel1_email_smtp_verified IS NOT TRUE
      AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome <> ''
      AND (o.nivel1_email IS NULL OR o.nivel1_email='')
    GROUP BY o.cnpj, ed.dominio
""")
targets=cur.fetchall()
print(f"Bucket 3 targets: {len(targets)}")

stats={'finder_ok':0,'role_ok':0,'no_match':0,'obras_upd':0}

for cnpj, dom, empresa, nome in targets:
    print(f"\n--- {nome} @ {dom} ({empresa}) ---")
    parts=nome.strip().split()
    parts=[p for p in parts if p.lower() not in ('da','de','do','dos','das','e','&','-')]
    if len(parts)<2:
        print(f"  skip: nome curto"); continue
    first=deaccent(parts[0]); last=deaccent(parts[-1])

    d=finder(dom, first, last)
    email=d.get('email'); score=d.get('score') or 0
    if email and score>=60:
        v=verify(email); st=v.get('status') or 'unknown'; sc2=v.get('score') or 0
        smtp_ok = st in ('valid','accept_all','webmail') and sc2>=50
        if not smtp_ok and st=='unknown' and sc2>=70:
            smtp_ok=True; st='unknown_accept'
        if smtp_ok:
            stats['finder_ok']+=1
            print(f"  ✓ FINDER: {email} ({st})")
            cur.execute("""
                INSERT INTO contatos_alternativos
                  (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_8c_finder')
                ON CONFLICT (email) DO NOTHING
            """, (cnpj, dom, email, nome, d.get('position') or '', score, st))
            cur.execute("""
                UPDATE obras
                SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                    nivel1_email_score=%s, nivel1_email_status=%s,
                    nivel1_origem_enrichment='V9_8c_finder',
                    decisor_status='DISCOVERED_V9_8C'
                WHERE cnpj=%s AND classificacao_computed='OURO'
                  AND nivel1_email_smtp_verified IS NOT TRUE
            """, (email, score, st, cnpj))
            stats['obras_upd']+=cur.rowcount
            print(f"  obras: {cur.rowcount}")
            conn.commit()
            time.sleep(1.5); continue
        else:
            print(f"  finder email invalido ({st})")
    else:
        print(f"  finder no email (score={score})")

    # Role-email fallback
    res=find_role(dom)
    if not res:
        stats['no_match']+=1
        print(f"  NO_MATCH"); time.sleep(0.5); continue
    email, role, st, sc = res
    cargo=role.title()
    stats['role_ok']+=1
    print(f"  ✓ ROLE: {email} ({st})")
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_8c_role')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, f'Equipe {cargo}', cargo, sc, st))
    cur.execute("""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_origem_enrichment='V9_8c_role',
            decisor_status='DISCOVERED_V9_8C'
        WHERE cnpj=%s AND classificacao_computed='OURO'
          AND nivel1_email_smtp_verified IS NOT TRUE
    """, (email, sc, st, cnpj))
    stats['obras_upd']+=cur.rowcount
    print(f"  obras: {cur.rowcount}")
    conn.commit()
    time.sleep(0.5)

print(f"\n=== STATS V9.8c ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
