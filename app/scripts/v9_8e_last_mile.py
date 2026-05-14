"""V9.8e — Last mile OURO: 6 NO_MATCH (14 obras)."""
import os, time, requests, psycopg2

HUNTER_KEY=os.environ['HUNTER_API_KEY']
SERPER_KEY=os.environ['SERPER_API_KEY']

ROLES_EXT=['compras','suprimentos','procurement','engenharia','projetos','manutencao',
       'operacoes','obras','contato','sourcing','licitacoes','licitacao','cgcl',
       'faleconosco','sac','atendimento','fornecedores','cadastrofornecedor',
       'gestaocontratos','gerencia']

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def find_role(dom, verbose=True):
    for role in ROLES_EXT:
        e=f"{role}@{dom}"
        d=verify(e); st=d.get('status') or 'unknown'; sc=d.get('score') or 0
        if verbose: print(f"    {e} → {st} {sc}")
        time.sleep(0.5)
        if st in ('valid','accept_all','webmail') and sc>=50:
            return e, role, st, sc
        if st=='unknown' and sc>=70:
            return e, role, 'unknown_accept', sc
    return None

def ds_personal(domain, seniority='senior,executive', limit=100):
    p={'domain':domain,'api_key':HUNTER_KEY,'limit':limit,'type':'personal'}
    if seniority: p['seniority']=seniority
    try:
        r=requests.get("https://api.hunter.io/v2/domain-search",params=p,timeout=30)
        return r.json().get('data',{}).get('emails',[])
    except: return []

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'wins':0,'no_match':[]}

# Mais combinações de domínios pros 6 NO_MATCH
TARGETS = [
    # (cnpj/None, empresa_pattern, label, [domains])
    ('44983435000411', None, 'Granel Quimica', ['granel-quimica.com.br','gqsa.com.br','grupogranel.com','granel.com','granelquimica.com']),
    (None, '%GRANDE SERTÃO%', 'Grande Sertao Transmissora', ['gste.com.br','statesgrid.com.br','stategridbr.com.br','sgbr.com.br']),
    (None, '%RIALMA%', 'Rialma', ['rialmasa.com.br','rialma.com','grupo-rialma.com.br','rialma.adm.br']),
    (None, '%Shopee%', 'Shopee', ['careers.shopee.com.br','shopee.sg','sea.com','seagroup.com','meugruposhopee.com.br']),
    ('53819657000303', None, 'Graca Aranha/State Grid', ['stategrid.com','sgcc.com.cn','stategrid.cn','chesf.com.br']),
    ('60498417000158', None, 'Metro SP', ['metrosp.com.br','licitacoes.metro.sp.gov.br','licitacao.metro.sp.gov.br']),
]

for cnpj, pat, label, alts in TARGETS:
    print(f"\n=== {label} ===")
    if cnpj:
        cur.execute("SELECT COUNT(*) FROM obras WHERE classificacao_computed='OURO' AND cnpj=%s AND nivel1_email_smtp_verified IS NOT TRUE", (cnpj,))
    else:
        cur.execute("SELECT COUNT(*) FROM obras WHERE classificacao_computed='OURO' AND cnpj IS NULL AND empresa ILIKE %s AND nivel1_email_smtp_verified IS NOT TRUE", (pat,))
    cnt=cur.fetchone()[0]
    print(f"  obras gap: {cnt}")
    if not cnt:
        continue

    found=None
    for d in alts:
        print(f"  >>> {d}")
        r=find_role(d, verbose=False)
        if r:
            found=(r, d)
            print(f"    ✓ role: {r[0]} ({r[2]})")
            break
    if not found:
        # Try DS senior across all alt domains
        for d in alts:
            print(f"  DS senior {d}")
            ems=ds_personal(d)
            print(f"    {len(ems)} emails")
            # take first with valid position
            for em in ems:
                ev=(em.get('value') or '').lower()
                if not ev: continue
                cargo=em.get('position') or ''
                if any(bad in cargo.lower() for bad in ['vp','president','marketing','sales','vendas','rh','legal','financ','treasur']):
                    continue
                v=verify(ev)
                st=v.get('status') or 'unknown'; sc=v.get('score') or 0
                if st in ('valid','accept_all','webmail') and sc>=50:
                    found=((ev, em.get('first_name','')+'_ds', st, sc), d)
                    # construct
                    found=((ev,'ds',st,sc),d)
                    print(f"    ✓ DS pick: {ev} ({cargo})")
                    break
            if found: break
            time.sleep(1)

    if not found:
        stats['no_match'].append(label); print(f"  ❌ STILL NO_MATCH"); continue

    (email, role, st, sc), dom = found
    cargo=role.title() if role!='ds' else 'Equipe (DS senior)'
    # build update
    if cnpj:
        cur.execute("""
            INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
                validacao_metodo, validacao_data)
            VALUES (%s,%s,%s,'manual_chat',4,'manual_chat_v9_8e',NOW())
            ON CONFLICT (cnpj) DO UPDATE SET dominio=EXCLUDED.dominio
        """, (cnpj, label, dom))
        cur.execute("""
            UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                nivel1_email_score=%s, nivel1_email_status=%s,
                nivel1_origem_enrichment='V9_8e_last_mile', decisor_status='DISCOVERED_V9_8E'
            WHERE classificacao_computed='OURO' AND cnpj=%s AND nivel1_email_smtp_verified IS NOT TRUE
        """, (email, sc, st, cnpj))
    else:
        # set CNPJ canonico via known mapping (use placeholder if not known)
        # for these we don't have CNPJ canonico, just set email per pattern.
        cur.execute("""
            UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                nivel1_email_score=%s, nivel1_email_status=%s,
                nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
                nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
                nivel1_origem_enrichment='V9_8e_last_mile', decisor_status='DISCOVERED_V9_8E'
            WHERE classificacao_computed='OURO' AND cnpj IS NULL AND empresa ILIKE %s
              AND nivel1_email_smtp_verified IS NOT TRUE
        """, (email, sc, st, f'Equipe {cargo}', cargo, pat))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}")
    conn.commit()

print(f"\n=== STATS V9.8e ===")
print(f"  wins: {stats['wins']}")
print(f"  no_match: {stats['no_match']}")
cur.close(); conn.close()
