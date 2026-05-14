"""V9.8f — PRATA last mile."""
import os, time, requests, psycopg2, re

HUNTER_KEY=os.environ['HUNTER_API_KEY']
SERPER_KEY=os.environ['SERPER_API_KEY']

P1=re.compile(r'\b(suprimentos?|compras|procurement|supply[\s-]?chain|sourcing|purchas\w*)\b',re.I)
P2=re.compile(r'\b(engenharia|engineer\w*|projetos?|projects?|manuten[cç][aã]o|maintenance|obras\b|constru[cç][aã]o|opera[cç][oõ]es|operations|industrial|planta\b|fabril|infraestrutura|t[eé]cnico|technical|facility)\b',re.I)
EXCLUDE=re.compile(r'\b(treasur\w*|risk\b|safety\b|chartering|business[\s-]?develop\w*|locomotive|audit\w*|compliance|jur[ií]dic\w*|legal\b|investor|communicat\w*|marketing|comercial\b|vendas\b|sales\b|rh\b|finan[cç]\w*|cfo|cto|coo|ceo|cmo|presidente|sustainab|sustentab|esg)\b',re.I)

ROLES_EXT=['compras','suprimentos','procurement','engenharia','projetos','manutencao',
       'operacoes','obras','contato','sourcing','licitacoes','licitacao','cgcl',
       'faleconosco','sac','atendimento','fornecedores','gestaocontratos','gerencia']

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def ds(domain, seniority='senior,executive', limit=100):
    p={'domain':domain,'api_key':HUNTER_KEY,'limit':limit,'type':'personal'}
    if seniority: p['seniority']=seniority
    try:
        r=requests.get("https://api.hunter.io/v2/domain-search",params=p,timeout=30)
        return r.json().get('data',{}).get('emails',[])
    except: return []

def pick(emails, skip):
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
    cands.sort(key=lambda x:(x[0],x[1],x[2]))
    return cands[0][3] if cands else None

def find_role(dom, verbose=False):
    for role in ROLES_EXT:
        e=f"{role}@{dom}"
        d=verify(e); st=d.get('status') or 'unknown'; sc=d.get('score') or 0
        if verbose: print(f"   {e} → {st} {sc}")
        time.sleep(0.4)
        if st in ('valid','accept_all','webmail') and sc>=50:
            return e, role, st, sc
        if st=='unknown' and sc>=70:
            return e, role, 'unknown_accept', sc
    return None

def scrape_serper_emails(query):
    """Procura em snippet/title por emails."""
    try:
        r=requests.post('https://google.serper.dev/search',
            headers={'X-API-KEY':SERPER_KEY,'Content-Type':'application/json'},
            json={'q':query,'hl':'pt','gl':'br','num':10}, timeout=15)
        results=r.json().get('organic',[])
    except: return []
    emails=set()
    pat=re.compile(r'[\w\.\-_]+@[\w\.\-]+\.[a-zA-Z]{2,}')
    for res in results:
        text=' '.join([res.get('title',''), res.get('snippet','')])
        emails.update(pat.findall(text))
    return list(emails)

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'wins':0,'no_match':[]}

# Strat A: Hunter DS senior + new alt domains
TARGETS = [
    ('60476884000187', None, 'Air Liquide', ['airliquide.com','br.airliquide.com','engineering.airliquide.com','airliquide.fr']),
    ('43843358001322', None, 'Air Products', ['airproducts.com','airproducts.com.br','airproducts.com.au','airproducts.io']),
    ('49695667000145', None, 'Porto Itaqui (EMAP)', ['emap.ma.gov.br','portodoitaqui.com.br','portoitaqui.com']),
    ('27383117000239', None, 'New Wave', ['newwave.com.br','grupobravonewwave.com.br','newwaveus.com']),
    ('18648563000156', None, 'Novo Porto Multicargas', ['multicargas.com.br','novoportoms.com.br','novoporto.com.br','multicargasms.com.br']),
    ('24358329000197', None, 'Santos STS', ['evolveinfraestrutura.com.br','evolveinfra.com.br','stssantos.com.br']),
    ('03110981000118', None, 'TUP EBT', ['ebt.com.br','tupebt.com.br','santorini.com.br','ebt.eng.br']),
    (None, '%MORRO DO IPÊ%', 'Morro do Ipê', ['morrodoipe.com.br','vetria.com.br','tcl-mining.com.br','tlc-mining.com']),
    (None, '%Mega Condom%', 'Mega Condominio (GLP)', ['glprop.com','glprop.com.br','glpbr.com.br','glprolp.com.br','prologis.com','prologis.com.br']),
]

for cnpj, pat, label, alts in TARGETS:
    print(f"\n=== {label} ===")

    # Build skip list
    skip=set()
    if cnpj:
        cur.execute("SELECT LOWER(email) FROM contatos_alternativos WHERE cnpj=%s", (cnpj,))
        skip={r[0] for r in cur.fetchall() if r[0]}
        cur.execute("SELECT DISTINCT LOWER(nivel1_email) FROM obras WHERE cnpj=%s AND nivel1_email IS NOT NULL", (cnpj,))
        skip|={r[0] for r in cur.fetchall() if r[0]}

    found=None
    for d in alts:
        # 1. Hunter DS senior
        print(f"  DS senior {d}")
        ems=ds(d)
        print(f"    {len(ems)} emails")
        best=pick(ems, skip)
        if best:
            email=best.get('value')
            v=verify(email); st=v.get('status') or 'unknown'; sc=v.get('score') or 0
            smtp_ok = st in ('valid','accept_all','webmail') and sc>=50
            if not smtp_ok and st=='unknown' and sc>=70: smtp_ok=True; st='unknown_accept'
            if smtp_ok:
                nome=((best.get('first_name') or '')+' '+(best.get('last_name') or '')).strip()
                cargo=best.get('position') or ''
                print(f"    ✓ DS pick: {nome} | {cargo} | {email} ({st})")
                found=((email, nome, cargo, sc, st), d, 'ds_senior')
                break

        # 2. Role emails
        print(f"  role-emails {d}")
        r=find_role(d)
        if r:
            email, role, st, sc = r
            print(f"    ✓ ROLE: {email} ({st})")
            found=((email, f'Equipe {role.title()}', role.title(), sc, st), d, 'role')
            break

    if not found:
        # Strat B: Scrape Serper for contact emails
        print(f"  scrape Serper '{label} contato email'")
        emails=scrape_serper_emails(f'"{label}" contato fale conosco email')
        emails+=scrape_serper_emails(f'"{label}" suprimentos compras email')
        emails=[e for e in emails if e.lower() not in skip and not any(x in e.lower() for x in ['noreply','no-reply','example.com','sentry.io'])]
        emails=list(set(emails))[:5]
        print(f"    scraped {len(emails)} emails")
        for e in emails:
            v=verify(e); st=v.get('status') or 'unknown'; sc=v.get('score') or 0
            print(f"    {e} → {st} {sc}")
            time.sleep(0.5)
            if st in ('valid','accept_all','webmail') and sc>=50:
                dom=e.split('@')[1]
                found=((e, 'Equipe (Scrape)', 'Contato', sc, st), dom, 'scrape')
                print(f"    ✓ scrape pick: {e}")
                break

    if not found:
        stats['no_match'].append(label); print(f"  ❌ NO_MATCH"); continue

    (email, nome, cargo, sc, st), dom, source = found
    if cnpj:
        cur.execute("""
            INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
                validacao_metodo, validacao_data)
            VALUES (%s,%s,%s,'manual_chat',4,'manual_chat_v9_8f',NOW())
            ON CONFLICT (cnpj) DO UPDATE SET dominio=EXCLUDED.dominio
        """, (cnpj, label, dom))
        cur.execute("""
            UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                nivel1_email_score=%s, nivel1_email_status=%s,
                nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
                nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
                nivel1_origem_enrichment='V9_8f_prata_last', decisor_status='DISCOVERED_V9_8F'
            WHERE classificacao_computed='PRATA' AND cnpj=%s
              AND nivel1_email_smtp_verified IS NOT TRUE
        """, (email, sc, st, nome, cargo, cnpj))
    else:
        cur.execute("""
            UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                nivel1_email_score=%s, nivel1_email_status=%s,
                nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
                nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
                nivel1_origem_enrichment='V9_8f_prata_last', decisor_status='DISCOVERED_V9_8F'
            WHERE classificacao_computed='PRATA' AND cnpj IS NULL AND empresa ILIKE %s
              AND nivel1_email_smtp_verified IS NOT TRUE
        """, (email, sc, st, nome, cargo, pat))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}")
    conn.commit()

print(f"\n=== STATS V9.8f ===")
print(f"  wins: {stats['wins']}")
print(f"  no_match: {stats['no_match']}")
cur.close(); conn.close()
