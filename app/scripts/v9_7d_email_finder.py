"""V9.7d — Email Finder nos 7 decisores PRATA com nome real mas sem email.
+ Domain Search pra ML (CD Logístico) e Nestlé.
"""
import os, time, requests, psycopg2, re
from unicodedata import normalize

HUNTER_KEY=os.environ['HUNTER_API_KEY']

NAMED = [
    ('03110981000118', 'ebt.com.br',                'Aquiles', 'Teixeira',    'Diretor Comercial — TUP EBT'),
    ('13970936000197', 'tepor.com.br',              'Fabiano', 'Crespo',      'Administrador TEPOR'),
    ('18091544000171', 'petrocitynavegacao.com.br', 'Jose',    'Silva',       'Presidente Petrocity Portos'),
    ('18648563000156', 'multicargas.com.br',        'Rossana', 'Cattalini',   'Sócia Novo Porto'),
    ('20391326000102', 'portocentral.com.br',       'Jose',    'Novaes',      'Diretor-Presidente Porto Central'),
    ('24358329000197', 'evolveinfraestrutura.com.br','Delvan', 'Monteiro',    'Diretor EVOLVE'),
    ('49695667000145', 'portodoitaqui.com.br',      'Gabriel', 'Cassia',      'Gerente Pesquisa Inovação EMAP'),
]
# ML e Nestlé: Domain Search fallback
DS_FALLBACK = [
    ('03007331000171', 'mercadolivre.com.br', 'Mercado Livre (Real Estate)'),
    ('60409075000152', 'nestle.com.br',       'Nestle (CAPEX/Engenharia)'),
]

def deaccent(s):
    return normalize('NFKD',s).encode('ascii','ignore').decode('ascii')

def hunter_finder(domain, first, last):
    try:
        r=requests.get("https://api.hunter.io/v2/email-finder",
            params={'domain':domain,'first_name':first,'last_name':last,
                    'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def hunter_verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def hunter_ds(domain, seniority='senior,executive', limit=100, type_='personal'):
    p={'domain':domain,'api_key':HUNTER_KEY,'limit':limit,'type':type_}
    if seniority: p['seniority']=seniority
    try:
        r=requests.get("https://api.hunter.io/v2/domain-search",params=p,timeout=30)
        return r.json().get('data',{}).get('emails',[])
    except: return []

EXCLUDE = re.compile(
    r'\b(treasur\w*|risk\b|safety\b|seguran[cç]a|chartering|fretamento|'
    r'business[\s-]?develop\w*|locomotive\w*|maquinista|conductor|'
    r'audit\w*|compliance|jur[ií]dic\w*|legal\b|counsel|advogad\w*|'
    r'investor|communicat\w*|comunic\w*|marketing|press|imprensa|midia|'
    r'rh\b|recursos[\s-]?humanos|cultura\b|finan[cç]\w*|finance|'
    r'sustainab\w*|sustentab\w*|esg\b)\b', re.I)
RELEVANT_ML = re.compile(r'(real[\s-]?estate|facility|facilities|opera[cç][oõ]es|operations|logistic|log[ií]stica|infrastructure|infraestrutura|engenharia|engineer|construc)', re.I)
RELEVANT_NESTLE = re.compile(r'(capex|engenharia|engineer|projetos?|projects?|infrastructure|infraestrutura|opera[cç][oõ]es|operations|industrial|planta\b|fabril|f[aá]brica|maintenance|manuten[cç][aã]o|technical|t[eé]cnic)', re.I)

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'finder_tried':0,'finder_ok':0,'ds_tried':0,'ds_ok':0,'obras_upd':0}

def update_obra(cnpj, nome, cargo, email, score, status, linkedin, dominio, origem_tag):
    smtp_ok = status in ('valid','accept_all','webmail')
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dominio, email, nome, cargo, score, status, origem_tag))
    cur.execute("""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=%s,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_linkedin=COALESCE(%s, nivel1_linkedin),
            decisor_status='DISCOVERED_V9_7D',
            nivel1_origem_enrichment=%s,
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s)
        WHERE cnpj=%s AND classificacao_computed='PRATA'
          AND nivel1_email IS NULL
    """, (email, smtp_ok, score, status, linkedin, origem_tag, cargo, nome, cnpj))
    return cur.rowcount, smtp_ok

for cnpj, dom, first, last, cargo in NAMED:
    print(f"\n--- {first} {last} @ {dom} ({cnpj}) ---")
    stats['finder_tried']+=1
    d=hunter_finder(dom, deaccent(first), deaccent(last))
    email=d.get('email')
    score=d.get('score') or 0
    print(f"  finder: {email} score={score}")
    if not email or score<60:
        print(f"  skip (no email or low score)")
        time.sleep(1); continue
    v=hunter_verify(email)
    status=v.get('status') or 'unknown'
    print(f"  verifier: {status} score={v.get('score')}")
    if status in ('valid','accept_all','webmail'):
        stats['finder_ok']+=1
    nome_full=f"{first} {last}"
    n, smtp_ok = update_obra(cnpj, nome_full, cargo, email, score, status, d.get('linkedin_url'), dom, 'V9_7d_email_finder')
    stats['obras_upd']+=n
    print(f"  obras updated: {n} (smtp_ok={smtp_ok})")
    conn.commit()
    time.sleep(2)

for cnpj, dom, label in DS_FALLBACK:
    print(f"\n--- DS fallback: {label} @ {dom} ({cnpj}) ---")
    stats['ds_tried']+=1
    emails=hunter_ds(dom, limit=100)
    print(f"  hunter returned {len(emails)} emails")
    relevant = RELEVANT_ML if 'mercadolivre' in dom else RELEVANT_NESTLE
    cands=[]
    for em in emails:
        cargo_em=em.get('position') or ''
        if EXCLUDE.search(cargo_em): continue
        if not relevant.search(cargo_em): continue
        if (em.get('confidence') or 0) < 70: continue
        cands.append(em)
    if not cands:
        print(f"  no relevant cargo. retry no seniority ...")
        emails=hunter_ds(dom, seniority=None, limit=100)
        for em in emails:
            cargo_em=em.get('position') or ''
            if EXCLUDE.search(cargo_em): continue
            if not relevant.search(cargo_em): continue
            if (em.get('confidence') or 0) < 70: continue
            cands.append(em)
    if not cands:
        print(f"  NO_MATCH")
        time.sleep(2); continue
    cands.sort(key=lambda e: -(e.get('confidence') or 0))
    best=cands[0]
    email=best.get('value')
    nome=((best.get('first_name') or '')+' '+(best.get('last_name') or '')).strip()
    cargo=best.get('position') or ''
    score=best.get('confidence') or 0
    linkedin=best.get('linkedin') or None
    print(f"  pick: {nome} | {cargo} | {email} | score={score}")
    v=hunter_verify(email)
    status=v.get('status') or 'unknown'
    print(f"  verifier: {status}")
    if status in ('valid','accept_all','webmail'):
        stats['ds_ok']+=1
    n, smtp_ok = update_obra(cnpj, nome, cargo, email, score, status, linkedin, dom, 'V9_7d_ds_fallback')
    stats['obras_upd']+=n
    print(f"  obras updated: {n} (smtp_ok={smtp_ok})")
    conn.commit()
    time.sleep(2)

print(f"\n=== STATS V9.7d ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
