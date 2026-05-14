"""V9.8b — Replace invalid emails OURO via Domain Search seniority + role-email.
- Para cada CNPJ com email invalid: Hunter DS senior/exec; se NO_MATCH, role emails.
- Para emails 'unknown' com score>=70: aceita como valid (Hunter throttle).
"""
import os, time, requests, psycopg2, re

HUNTER_KEY=os.environ['HUNTER_API_KEY']

P1=re.compile(r'\b(suprimentos?|compras|procurement|supply[\s-]?chain|sourcing|aquisi[cç][aã]o|purchas\w*|buyer)\b',re.I)
P2=re.compile(r'\b(engenharia|engineer\w*|projetos?|projects?|manuten[cç][aã]o|maintenance|implanta[cç][aã]o|obras\b|constru[cç][aã]o|construction|epc\b|civil\b|opera[cç][oõ]es|operations|industrial|planta\b|fabril|f[aá]brica|infraestrutura|infrastructure|t[eé]cnico|technical|facility|facilities)\b',re.I)
EXCLUDE=re.compile(r'\b(treasur\w*|risk\b|safety\b|chartering|fretamento|business[\s-]?develop\w*|locomotive\w*|audit\w*|compliance|jur[ií]dic\w*|legal\b|counsel|advogad\w*|investor|communicat\w*|comunic\w*|marketing|comercial\b|vendas\b|sales\b|revenue|press|imprensa|midia|rh\b|recursos[\s-]?humanos|cultura\b|finan[cç]\w*|finance|cfo\b|cto\b|coo\b|cmo\b|ceo\b|presidente|vice[\s-]?presidente|vp\b|sustainab\w*|sustentab\w*|esg\b)\b',re.I)

ROLES=['compras','suprimentos','procurement','engenharia','projetos','manutencao',
       'operacoes','obras','contato','sourcing','licitacoes','licitacao']

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def ds(domain, seniority='senior,executive', limit=100, type_='personal'):
    p={'domain':domain,'api_key':HUNTER_KEY,'limit':limit,'type':type_}
    if seniority: p['seniority']=seniority
    try:
        r=requests.get("https://api.hunter.io/v2/domain-search",params=p,timeout=30)
        return r.json().get('data',{}).get('emails',[])
    except: return []

def pick_best(emails, skip):
    cands=[]
    for em in emails:
        e=(em.get('value') or '').lower()
        if not e or e in skip: continue
        s=em.get('confidence') or 0
        if s<70: continue
        c=em.get('position') or ''
        if EXCLUDE.search(c): continue
        if P1.search(c): t=1
        elif P2.search(c): t=2
        else: continue
        cands.append((t,-s,e,em))
    if not cands: return None
    cands.sort(key=lambda x:(x[0],x[1],x[2]))
    return cands[0][3]

def find_role(domain):
    for role in ROLES:
        e=f"{role}@{domain}"
        d=verify(e)
        st=d.get('status') or 'unknown'
        sc=d.get('score') or 0
        time.sleep(0.6)
        if st in ('valid','accept_all','webmail') and sc>=50:
            return e, role, st, sc
        if st=='unknown' and sc>=70:
            return e, role, 'unknown_accept', sc
    return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

# Step 1: aceitar emails 'unknown' com score>=70 como valid
cur.execute("""
    UPDATE obras
    SET nivel1_email_smtp_verified=TRUE
    WHERE classificacao_computed='OURO'
      AND nivel1_email_smtp_verified IS NOT TRUE
      AND nivel1_email_status='unknown'
      AND nivel1_email_score>=70
""")
unknown_accept=cur.rowcount
print(f"Unknown accept (score>=70): {unknown_accept} obras")
conn.commit()

# Step 2: para CNPJs com email ainda invalid, replace
cur.execute("""
    SELECT DISTINCT o.cnpj, MAX(ed.dominio) AS dom
    FROM obras o JOIN empresa_dominios ed ON o.cnpj=ed.cnpj
    WHERE o.classificacao_computed='OURO'
      AND o.nivel1_email_smtp_verified IS NOT TRUE
      AND o.nivel1_email IS NOT NULL AND o.nivel1_email <> ''
      AND ed.dominio IS NOT NULL
    GROUP BY o.cnpj
""")
targets=cur.fetchall()
print(f"\nCNPJs com email invalid + dominio: {len(targets)}")

stats={'ds_hit':0,'role_hit':0,'no_match':0,'obras_upd':0}

for cnpj, dom in targets:
    print(f"\n--- {cnpj} @ {dom} ---")

    cur.execute("""
        SELECT LOWER(email) FROM contatos_alternativos WHERE cnpj=%s OR empresa_dominio=%s
    """, (cnpj, dom))
    skip={r[0] for r in cur.fetchall() if r[0]}
    cur.execute("SELECT DISTINCT LOWER(nivel1_email) FROM obras WHERE cnpj=%s AND nivel1_email IS NOT NULL", (cnpj,))
    skip |= {r[0] for r in cur.fetchall() if r[0]}

    # Try DS senior
    emails=ds(dom)
    best=pick_best(emails, skip)
    source='ds_senior'
    if not best:
        emails=ds(dom, seniority=None)
        best=pick_best(emails, skip)
        source='ds_all'
    if best:
        email=best.get('value')
        v=verify(email)
        st=v.get('status') or 'unknown'
        sc=v.get('score') or 0
        smtp_ok = st in ('valid','accept_all','webmail') and sc>=50
        if not smtp_ok and st=='unknown' and sc>=70:
            smtp_ok=True; st='unknown_accept'
        if smtp_ok:
            stats['ds_hit']+=1
            nome=((best.get('first_name') or '')+' '+(best.get('last_name') or '')).strip()
            cargo=best.get('position') or ''
            print(f"  ✓ DS: {nome} | {cargo} | {email} ({st})")
            cur.execute("""
                INSERT INTO contatos_alternativos
                  (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_8b_ds')
                ON CONFLICT (email) DO NOTHING
            """, (cnpj, dom, email, nome, cargo, sc, st))
            cur.execute("""
                UPDATE obras
                SET nivel1_nome=%s, nivel1_cargo=%s, nivel1_email=%s,
                    nivel1_email_smtp_verified=TRUE,
                    nivel1_email_score=%s, nivel1_email_status=%s,
                    decisor_status='DISCOVERED_V9_8B',
                    nivel1_origem_enrichment='V9_8b_ds'
                WHERE cnpj=%s AND classificacao_computed='OURO'
                  AND nivel1_email_smtp_verified IS NOT TRUE
            """, (nome, cargo, email, sc, st, cnpj))
            stats['obras_upd']+=cur.rowcount
            print(f"  obras: {cur.rowcount}")
            conn.commit()
            time.sleep(1.5)
            continue

    # Role-email fallback
    print(f"  role fallback @ {dom}")
    res=find_role(dom)
    if not res:
        stats['no_match']+=1
        print(f"  NO_MATCH")
        time.sleep(0.5); continue
    email, role, st, sc = res
    cargo=role.title()
    stats['role_hit']+=1
    print(f"  ✓ ROLE: {email} ({st})")
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_8b_role')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, f'Equipe {cargo}', cargo, sc, st))
    cur.execute("""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_origem_enrichment='V9_8b_role',
            decisor_status='DISCOVERED_V9_8B'
        WHERE cnpj=%s AND classificacao_computed='OURO'
          AND nivel1_email_smtp_verified IS NOT TRUE
    """, (email, sc, st, cnpj))
    stats['obras_upd']+=cur.rowcount
    print(f"  obras: {cur.rowcount}")
    conn.commit()
    time.sleep(0.5)

print(f"\n=== STATS V9.8b ===")
print(f"  unknown_accept: {unknown_accept}")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
