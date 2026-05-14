"""V9.8g — Serper scrape pros 5 PRATA residuais. Extrai emails do site oficial,
verifica via Hunter."""
import os, time, requests, psycopg2, re, sys

HUNTER_KEY=os.environ['HUNTER_API_KEY']
SERPER_KEY=os.environ['SERPER_API_KEY']

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def serper(q, num=10):
    try:
        r=requests.post('https://google.serper.dev/search',
            headers={'X-API-KEY':SERPER_KEY,'Content-Type':'application/json'},
            json={'q':q,'hl':'pt','gl':'br','num':num}, timeout=20)
        return r.json().get('organic',[])
    except: return []

def fetch(url):
    try:
        r=requests.get(url, timeout=15, headers={'User-Agent':'Mozilla/5.0'})
        return r.text
    except: return ''

EMAIL_RX=re.compile(r'[\w\.\-_]+@[\w\.\-]+\.[a-zA-Z]{2,}')

def extract_emails(text, domain_hint=None):
    emails=set()
    for e in EMAIL_RX.findall(text or ''):
        e=e.lower()
        if any(b in e for b in ['noreply','no-reply','example.com','sentry.io',
                                'wixpress','squarespace','wordpress','example@',
                                'seudominio','seuemail','seunome','png','jpg','svg','webp']):
            continue
        if domain_hint and domain_hint not in e:
            continue
        emails.add(e)
    return list(emails)

TARGETS=[
    (None, '%MORRO DO IPÊ%', 'Mineração Morro do Ipê', 'morrodoipe.com.br'),
    (None, '%Mega Condom%', 'Mega Condomínio Logístico GLP', 'glp.com'),
    ('27383117000239', None, 'New Wave', 'newwave.com.br'),
    ('18648563000156', None, 'Novo Porto Multicargas', 'multicargas.com.br'),
    ('03110981000118', None, 'TUP EBT Santorini', 'ebt.com.br'),
]

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'wins':0,'no_match':[]}

for cnpj, pat, label, dom_hint in TARGETS:
    print(f"\n=== {label} ===", flush=True)

    # Build skip
    skip=set()
    if cnpj:
        cur.execute("SELECT LOWER(email) FROM contatos_alternativos WHERE cnpj=%s", (cnpj,))
        skip={r[0] for r in cur.fetchall() if r[0]}
        cur.execute("SELECT DISTINCT LOWER(nivel1_email) FROM obras WHERE cnpj=%s AND nivel1_email IS NOT NULL", (cnpj,))
        skip|={r[0] for r in cur.fetchall() if r[0]}

    # 1. Serper buscar contato + extrair emails do snippet
    queries=[
        f'"{label}" suprimentos compras email contato',
        f'"{label}" fale conosco contato email',
        f'site:{dom_hint} contato',
        f'site:{dom_hint} fornecedores',
        f'site:{dom_hint} compras',
    ]
    candidates=set()
    contact_urls=[]
    for q in queries:
        print(f"  query: {q}", flush=True)
        items=serper(q)
        for it in items[:8]:
            text=(it.get('title','')+' '+it.get('snippet','')+' '+it.get('link',''))
            for e in extract_emails(text):
                if e not in skip:
                    candidates.add(e)
            link=it.get('link','')
            if dom_hint in link and ('contato' in link.lower() or 'fale' in link.lower() or 'fornecedor' in link.lower() or 'compra' in link.lower() or 'suprimento' in link.lower()):
                contact_urls.append(link)
        time.sleep(0.5)

    # 2. Fetch up to 3 contact URLs e extrair emails
    print(f"  contact URLs: {contact_urls[:3]}", flush=True)
    for url in contact_urls[:3]:
        html=fetch(url)
        for e in extract_emails(html, domain_hint=dom_hint.split('.')[0]):  # filtra pelo "core" do dominio
            if e not in skip:
                candidates.add(e)
        time.sleep(0.5)

    print(f"  candidates: {sorted(candidates)[:10]}", flush=True)

    found=None
    for email in sorted(candidates):
        if email in skip: continue
        d=verify(email); st=d.get('status') or 'unknown'; sc=d.get('score') or 0
        print(f"    {email} → {st} {sc}", flush=True)
        time.sleep(0.5)
        if st in ('valid','accept_all','webmail') and sc>=50:
            found=(email, st, sc); break
        if st=='unknown' and sc>=70:
            found=(email, 'unknown_accept', sc); break

    if not found:
        stats['no_match'].append(label); print(f"  ❌ NO_MATCH", flush=True); continue

    email, st, sc = found
    dom = email.split('@')[1]
    print(f"  ✓ {email} ({st})", flush=True)
    if cnpj:
        cur.execute("""
            UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                nivel1_email_score=%s, nivel1_email_status=%s,
                nivel1_origem_enrichment='V9_8g_scrape', decisor_status='DISCOVERED_V9_8G'
            WHERE classificacao_computed='PRATA' AND cnpj=%s
              AND nivel1_email_smtp_verified IS NOT TRUE
        """, (email, sc, st, cnpj))
    else:
        cur.execute("""
            UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                nivel1_email_score=%s, nivel1_email_status=%s,
                nivel1_origem_enrichment='V9_8g_scrape', decisor_status='DISCOVERED_V9_8G'
            WHERE classificacao_computed='PRATA' AND cnpj IS NULL AND empresa ILIKE %s
              AND nivel1_email_smtp_verified IS NOT TRUE
        """, (email, sc, st, pat))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}", flush=True)
    conn.commit()

print(f"\n=== STATS V9.8g wins={stats['wins']} no_match={stats['no_match']} ===", flush=True)
cur.close(); conn.close()
