"""V9.8d — Final push OURO: 4 buckets em uma só passada.
- Bucket 4 (9): alt domains globais (basf.com, evonik.com, saint-gobain.com)
- Bucket 1 (4 multinacionais OPEX): map dominio canonico + role
- Bucket 0 (10 empresas cnpj NULL): canonico CNPJ + domain + role
- Bucket 3 (2): Email Finder + DS
"""
import os, time, requests, psycopg2
from unicodedata import normalize

HUNTER_KEY=os.environ['HUNTER_API_KEY']
ROLES=['compras','suprimentos','procurement','engenharia','projetos','manutencao',
       'operacoes','obras','contato','sourcing','licitacoes','licitacao','suprimento']

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
def find_role(domain, verbose=False):
    for role in ROLES:
        e=f"{role}@{domain}"
        d=verify(e); st=d.get('status') or 'unknown'; sc=d.get('score') or 0
        if verbose: print(f"    {e} → {st} {sc}")
        time.sleep(0.5)
        if st in ('valid','accept_all','webmail') and sc>=50:
            return e, role, st, sc
        if st=='unknown' and sc>=70:
            return e, role, 'unknown_accept', sc
    return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

def upsert_dom(cnpj, name, dom, method='manual_chat_v9_8d'):
    cur.execute("""
        INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
            validacao_metodo, validacao_data)
        VALUES (%s,%s,%s,'manual_chat',4,%s,NOW())
        ON CONFLICT (cnpj) DO UPDATE SET dominio=EXCLUDED.dominio, validacao_data=NOW()
    """, (cnpj, name, dom, method))

def insert_contato(cnpj, dom, email, nome, cargo, score, status, origem):
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, nome, cargo, score, status, origem))

stats={'wins':0,'no_match':[]}

# === Bucket 4: alt domain probe ===
ALT_DOMS = [
    ('62695036000194', ['evonik.com','evonik.com.br'],        'Evonik'),
    ('01413969000742', ['gruposm.com.br','comfrio.com.br'],   'Comfrio'),
    ('44983435000411', ['granelquimica.com.br','granel.com.br','grupogranel.com.br'], 'Granel Quimica'),
    ('25311224000145', ['quataalimentos.com.br','qualityalimentosba.com.br','quatabh.com.br'], 'Quality Alimentos'),
    ('61064838000133', ['saint-gobain.com','sg-glassolutions.com.br','saint-gobain.com.br'], 'Saint-Gobain'),
    ('05835276000103', ['ozminerals.com','riominerals.com','bhp.com'], 'Rio Minerals (oz/BHP)'),
    ('48539407000118', ['basf.com','basf.com.br'], 'BASF'),
    ('16978568000111', ['pagold.com.br','fggoldmining.com'], 'GF Gold'),
]
for cnpj, alts, label in ALT_DOMS:
    print(f"\n=== B4 {label} ({cnpj}) ===")
    found=None
    for d in alts:
        print(f"  try {d}")
        r=find_role(d)
        if r: found=(r,d); break
    if not found:
        stats['no_match'].append(label); print(f"  NO_MATCH"); continue
    (email, role, st, sc), dom = found
    cargo=role.title()
    print(f"  ✓ {email} ({st})")
    upsert_dom(cnpj, label, dom)
    insert_contato(cnpj, dom, email, f'Equipe {cargo}', cargo, sc, st, 'V9_8d_alt_b4')
    cur.execute("""
        UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_origem_enrichment='V9_8d_alt_b4', decisor_status='DISCOVERED_V9_8D'
        WHERE cnpj=%s AND classificacao_computed='OURO'
          AND nivel1_email_smtp_verified IS NOT TRUE
    """, (email, sc, st, cnpj))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}")
    conn.commit()

# === Bucket 1: 4 multinacionais OPEX ===
B1 = [
    ('43636416000103', 'Gen Fertilizantes',     ['genfertilizantes.com.br','gennitrogenados.com.br','heringerfertilizantes.com.br']),
    ('31452113000585', 'Clariant',              ['clariant.com','clariant.com.br']),
    ('16716929000585', 'Equilibrio Fertilizantes', ['equilibriofertilizantes.com.br','equilibrioagro.com.br']),
    ('05781654000113', 'Brasil Quimica',        ['brasilquimica.com.br','grupobq.com.br']),
]
for cnpj, label, alts in B1:
    print(f"\n=== B1 {label} ({cnpj}) ===")
    found=None
    for d in alts:
        print(f"  try {d}")
        r=find_role(d)
        if r: found=(r,d); break
    if not found:
        stats['no_match'].append(label); print(f"  NO_MATCH"); continue
    (email, role, st, sc), dom = found
    cargo=role.title()
    print(f"  ✓ {email} ({st})")
    upsert_dom(cnpj, label, dom)
    insert_contato(cnpj, dom, email, f'Equipe {cargo}', cargo, sc, st, 'V9_8d_b1')
    cur.execute("""
        UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            nivel1_origem_enrichment='V9_8d_b1', decisor_status='DISCOVERED_V9_8D'
        WHERE cnpj=%s AND classificacao_computed='OURO'
          AND nivel1_email_smtp_verified IS NOT TRUE
    """, (email, sc, st, f'Equipe {cargo}', cargo, cnpj))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}")
    conn.commit()

# === Bucket 0: empresa NULL cnpj ===
B0 = [
    ('%GRANDE SERTÃO%',  '32651395000148', 'Grande Sertao Transmissora', ['stategrid.com.br','grandesertaotransmissora.com.br']),
    ('%RIALMA%',         '03002475000170', 'Rialma Administracao', ['rialma.com.br']),
    ('%Equiplex%',       '57523392000147', 'Equiplex Farmaceutica', ['equiplex.com.br']),
    ('%Kley Hertz%',     '92695511000164', 'Kley Hertz Farmaceutica', ['kleyhertz.com.br']),
    ('%Kurashiki%',      '02565869000120', 'Kurashiki Chemical', ['kuraray.com','kurashiki.com.br','kurabo.co.jp']),
    ('%Shopee%',         '40432544000147', 'Shopee Brasil', ['shopee.com.br','shopee.com']),
    ('%Schmersal%',      '61854147000133', 'Schmersal Brasil', ['schmersal.com','schmersal.com.br']),
    ('%TERMINAL DE CONTÊINERES DE PARANAGUÁ%','01415519000150','TCP Paranagua', ['tcp.com.br','tcponline.com.br']),
    ('%DHL%',            '44781658000146', 'DHL Supply Chain', ['dhl.com','dhl-supply-chain.com','dhlbr.com.br']),
]
for pat, cnpj, label, alts in B0:
    print(f"\n=== B0 {label} (pat={pat}) ===")
    cur.execute("SELECT COUNT(*) FROM obras WHERE classificacao_computed='OURO' AND cnpj IS NULL AND empresa ILIKE %s AND nivel1_email_smtp_verified IS NOT TRUE", (pat,))
    cnt=cur.fetchone()[0]
    if cnt==0:
        print(f"  no obras match"); continue
    print(f"  obras matching: {cnt}")
    found=None
    for d in alts:
        print(f"  try {d}")
        r=find_role(d)
        if r: found=(r,d); break
    if not found:
        stats['no_match'].append(label); print(f"  NO_MATCH"); continue
    (email, role, st, sc), dom = found
    cargo=role.title()
    print(f"  ✓ {email} ({st})")
    upsert_dom(cnpj, label, dom)
    insert_contato(cnpj, dom, email, f'Equipe {cargo}', cargo, sc, st, 'V9_8d_b0')
    cur.execute("""
        UPDATE obras
        SET cnpj=%s,
            nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            nivel1_origem_enrichment='V9_8d_b0', decisor_status='DISCOVERED_V9_8D'
        WHERE classificacao_computed='OURO' AND cnpj IS NULL AND empresa ILIKE %s
          AND nivel1_email_smtp_verified IS NOT TRUE
    """, (cnpj, email, sc, st, f'Equipe {cargo}', cargo, pat))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}")
    conn.commit()

# === Bucket 3 residual: Email Finder ===
B3=[
    ('53819657000303', 'stategrid.com.br', 'Raimundo','Dantas', 'Graça Aranha/State Grid'),
    ('60498417000158', 'metro.sp.gov.br',  'Rogerio', 'Costa',  'Metrô SP'),
]
for cnpj, dom, first, last, label in B3:
    print(f"\n=== B3 {label}: {first} {last} @ {dom} ===")
    d=finder(dom, deaccent(first), deaccent(last))
    email=d.get('email'); sc=d.get('score') or 0
    if email and sc>=60:
        v=verify(email); st=v.get('status') or 'unknown'; sc2=v.get('score') or 0
        smtp_ok = st in ('valid','accept_all','webmail') and sc2>=50
        if not smtp_ok and st=='unknown' and sc2>=70:
            smtp_ok=True; st='unknown_accept'
        if smtp_ok:
            print(f"  ✓ FINDER {email} ({st})")
            cur.execute("""
                UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
                    nivel1_email_score=%s, nivel1_email_status=%s,
                    nivel1_origem_enrichment='V9_8d_finder'
                WHERE cnpj=%s AND classificacao_computed='OURO'
                  AND nivel1_email_smtp_verified IS NOT TRUE
            """, (email, sc, st, cnpj))
            n=cur.rowcount; stats['wins']+=n
            print(f"  obras: {n}")
            conn.commit(); time.sleep(1); continue
    # fallback role
    r=find_role(dom)
    if not r:
        stats['no_match'].append(label); print(f"  NO_MATCH"); continue
    email, role, st, sc = r
    cargo=role.title()
    print(f"  ✓ ROLE {email} ({st})")
    cur.execute("""
        UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_origem_enrichment='V9_8d_role'
        WHERE cnpj=%s AND classificacao_computed='OURO' AND nivel1_email_smtp_verified IS NOT TRUE
    """, (email, sc, st, cnpj))
    n=cur.rowcount; stats['wins']+=n; print(f"  obras: {n}")
    conn.commit()

print(f"\n=== STATS V9.8d ===")
print(f"  wins (obras): {stats['wins']}")
print(f"  no_match: {len(stats['no_match'])} - {stats['no_match']}")
cur.close(); conn.close()
