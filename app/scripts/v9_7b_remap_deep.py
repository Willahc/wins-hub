"""V9.7b — Corrige 4 mappings errados em empresa_dominios + clear nivel1_* +
re-roda V9.6 deep search + Email Verifier."""
import os, time, requests, psycopg2, re

HUNTER_KEY = os.environ['HUNTER_API_KEY']

REMAPS = [
    # cnpj, novo_dominio, empresa_nome
    ('04892707000100', 'dnit.gov.br',          'DNIT'),
    ('42278291000124', 'loginlogistica.com.br','LOG-IN Logistica Intermodal S/A'),
    ('03983431000103', 'enerpeixe.com.br',     'Enerpeixe S.A. (EDP)'),
    ('33541368000116', 'axia.com.br',          'AXIA Energia Nordeste S.A.'),
]

P1 = re.compile(
    r'\b(suprimentos?|compras|procurement|supply[\s-]?chain|sourcing|aquisi[cç][aã]o|'
    r'purchas\w*|buyer|comprador|licita[cç][aã]o|licit\w*)\b', re.I)
P2 = re.compile(
    r'\b(engenharia|engineer\w*|projetos?|projects?|manuten[cç][aã]o|maintenance|'
    r'implanta[cç][aã]o|obras\b|constru[cç][aã]o|construction|epc\b|civil\b|'
    r'opera[cç][oõ]es|operations\b|industrial|planta\b|plant[\s-]?manager|'
    r'fabril|f[aá]brica|infraestrutura|infrastructure|t[eé]cnico|t[eé]cnica|technical|'
    r'facility|facilities|asset\s+manag|ativos\b|superintendente|superintendent)\b', re.I)

EXCLUDE = re.compile(
    r'\b(treasur\w*|risk\b|safety\b|seguran[cç]a|chartering|frete\w*|fretamento|'
    r'business[\s-]?develop\w*|\bbd\b|locomotive\w*|maquinista|conductor|motorista|'
    r'audit\w*|compliance|jur[ií]dic\w*|legal\b|counsel|advogad\w*|'
    r'investor|rela[cç][aã]o[\s-]?\w*investid\w*|communicat\w*|comunic\w*|'
    r'marketing|comercial\b|vendas\b|sales\b|revenue|press|imprensa|midia|'
    r'rh\b|recursos[\s-]?humanos|human[\s-]?resources|people\b|cultura\b|'
    r'finan[cç]\w*|finance|cfo\b|cto\b|coo\b|cmo\b|ceo\b|'
    r'presidente|vice[\s-]?presidente|vp\b|sustainab\w*|sustentab\w*|esg\b)\b', re.I)

def cargo_ok(c):
    if not c: return None
    if EXCLUDE.search(c): return None
    if P1.search(c): return 1
    if P2.search(c): return 2
    return None

def pick_best(emails, skip):
    cands=[]
    for em in emails:
        e=(em.get('value') or '').lower()
        if not e or e in skip: continue
        s=em.get('confidence') or 0
        if s<70: continue
        c=em.get('position') or ''
        t=cargo_ok(c)
        if not t: continue
        cands.append((t,-s,e,em))
    if not cands: return None
    cands.sort(key=lambda x:(x[0],x[1],x[2]))
    return cands[0][3]

def hunter_domain_search(domain, limit=100, seniority='senior,executive', type_='personal'):
    p={'domain':domain,'api_key':HUNTER_KEY,'limit':limit,'type':type_}
    if seniority: p['seniority']=seniority
    try:
        r=requests.get("https://api.hunter.io/v2/domain-search",params=p,timeout=30)
        j=r.json()
        if 'errors' in j:
            print(f"  hunter errors: {j['errors']}")
            return []
        return j.get('data',{}).get('emails',[])
    except Exception as e:
        print(f"  ERR: {e}"); return []

def hunter_verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        j=r.json()
        if 'errors' in j: return None
        return j.get('data',{})
    except: return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'remapped':0,'cleared':0,'replaced':0,'no_match':0,'verified_valid':0}

for cnpj, novo_dom, empresa in REMAPS:
    print(f"\n=== {empresa} ({cnpj}) ===")
    print(f"  Remap dominio → {novo_dom}")

    cur.execute("""
        INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
            validacao_metodo, validacao_data)
        VALUES (%s, %s, %s, 'manual_chat', 5, 'manual_chat_v9_7_remap', NOW())
        ON CONFLICT (cnpj) DO UPDATE
          SET dominio=EXCLUDED.dominio, empresa_nome=EXCLUDED.empresa_nome,
              fonte=EXCLUDED.fonte, confianca=EXCLUDED.confianca,
              validacao_metodo=EXCLUDED.validacao_metodo, validacao_data=NOW()
    """, (cnpj, empresa, novo_dom))
    stats['remapped']+=1

    cur.execute("""
        UPDATE obras
        SET nivel1_nome=NULL, nivel1_cargo=NULL, nivel1_email=NULL,
            nivel1_email_smtp_verified=NULL, nivel1_email_score=NULL,
            nivel1_email_status=NULL, nivel1_linkedin=NULL,
            decisor_status=NULL,
            nivel1_origem_enrichment='V9_7_cleared_remap'
        WHERE cnpj=%s AND classificacao_computed='PRATA'
    """, (cnpj,))
    n=cur.rowcount
    stats['cleared']+=n
    print(f"  cleared {n} obras")
    conn.commit()

    cur.execute("""
        SELECT LOWER(email) FROM contatos_alternativos
        WHERE cnpj=%s OR empresa_dominio=%s
    """, (cnpj, novo_dom))
    skip={r[0] for r in cur.fetchall() if r[0]}

    print(f"  Hunter Domain Search ({novo_dom}, senior,executive, limit=100) ...")
    emails=hunter_domain_search(novo_dom)
    print(f"     returned {len(emails)} emails")
    best=pick_best(emails, skip)

    if not best:
        # try without seniority filter
        print(f"  retry sem seniority ...")
        emails=hunter_domain_search(novo_dom, seniority=None)
        best=pick_best(emails, skip)

    if not best:
        # try generic role-based
        print(f"  fallback type=generic ...")
        emails=hunter_domain_search(novo_dom, seniority=None, type_='generic', limit=50)
        role_kw=re.compile(r'(compras|suprimentos|procurement|sourcing|engenharia|projetos?|manuten[cç][aã]o|obras|opera[cç][oõ]es|licita)', re.I)
        for em in emails:
            e=(em.get('value') or '').lower()
            if not e or e in skip: continue
            local=e.split('@')[0]
            if not role_kw.search(local): continue
            if (em.get('confidence') or 0) < 50: continue
            em['position']=em.get('position') or f'role-email:{local}'
            best=em
            break

    if not best:
        stats['no_match']+=1
        print(f"  ↯ NO_MATCH em A+B+C")
        time.sleep(2); continue

    nome=((best.get('first_name') or '')+' '+(best.get('last_name') or '')).strip()
    cargo=best.get('position') or ''
    email=best.get('value')
    score=best.get('confidence') or 0
    linkedin=best.get('linkedin') or None
    print(f"  PICK: {nome or '(role)'} | {cargo} | {email} | score={score}")
    stats['replaced']+=1

    v=hunter_verify(email)
    verif_status=(v or {}).get('status') or 'unknown'
    smtp_ok=verif_status in ('valid','accept_all','webmail')
    print(f"  verifier: status={verif_status} smtp_ok={smtp_ok}")
    if smtp_ok: stats['verified_valid']+=1

    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, departamento, linkedin_url,
           hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'V9_7_remap_deep')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, novo_dom, email, nome, cargo, best.get('department') or '', linkedin, score, verif_status))

    cur.execute("""
        UPDATE obras
        SET nivel1_nome=%s, nivel1_cargo=%s, nivel1_email=%s,
            nivel1_email_smtp_verified=%s, nivel1_email_score=%s,
            nivel1_email_status=%s,
            decisor_status='DISCOVERED_V9_7',
            nivel1_origem_enrichment='V9_7_remap_deep',
            nivel1_linkedin=%s
        WHERE cnpj=%s
          AND classificacao_computed='PRATA'
          AND nivel1_origem_enrichment='V9_7_cleared_remap'
    """, (nome, cargo, email, smtp_ok, score, verif_status, linkedin, cnpj))
    print(f"  obras updated: {cur.rowcount}")
    conn.commit()
    time.sleep(2)

print(f"\n=== STATS V9.7b ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
