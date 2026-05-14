"""V9.7h — Limpar 18 residuais.
A) Re-verify 5 V2_verifier emails que nunca passaram pelo Email Verifier.
B) Role emails em domínios alternativos (Mahindra, ML, Nestlé).
C) Clear Mega Condomínio bad email + retry role.
D) Email Finder pros decisores com nome real e sem email.
"""
import os, time, requests, psycopg2
from unicodedata import normalize

HUNTER_KEY=os.environ['HUNTER_API_KEY']

def deaccent(s):
    return normalize('NFKD',s).encode('ascii','ignore').decode('ascii')

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

ROLES=['compras','suprimentos','procurement','engenharia','projetos','manutencao',
       'operacoes','obras','contato','sourcing']

def find_role(dom):
    for role in ROLES:
        e=f"{role}@{dom}"
        d=verify(e)
        st=d.get('status') or 'unknown'
        sc=d.get('score') or 0
        time.sleep(0.7)
        if st in ('valid','accept_all','webmail') and sc>=50:
            return e, role, st, sc
    return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'reverified_valid':0,'role_alt_found':0,'finder_found':0,'obras_upd':0}

# --- A) Re-verify V2_verifier emails que nunca foram revalidados ---
REVERIFY = [
    ('43843358001322', 'manutencao.br@airproducts.com',     'Air Products'),
    ('07526557000100', 'ricardo.oliveira@ambev.com.br',     'Ambev'),
    ('16404287000155', 'mario.souza@arauco.com',            'Arauco/Sucuriu'),
    (None,             'paulo.sales@moura.com.br',          'Baterias Moura'),
]
for cnpj, email, label in REVERIFY:
    print(f"\n--- REVERIFY {label} → {email} ---")
    d=verify(email)
    st=d.get('status') or 'unknown'
    sc=d.get('score') or 0
    print(f"  status={st} score={sc}")
    smtp_ok=st in ('valid','accept_all','webmail') and sc>=50
    if smtp_ok: stats['reverified_valid']+=1
    where_cnpj = "cnpj=%s" if cnpj else "cnpj IS NULL"
    args = ([smtp_ok, st, sc, cnpj, email] if cnpj else [smtp_ok, st, sc, email])
    sql = f"""
        UPDATE obras SET nivel1_email_smtp_verified=%s, nivel1_email_status=%s,
            nivel1_email_score=GREATEST(COALESCE(nivel1_email_score,0), %s)
        WHERE classificacao_computed='PRATA' AND {where_cnpj} AND nivel1_email=%s
    """
    cur.execute(sql, args)
    print(f"  obras updated: {cur.rowcount}")
    if smtp_ok: stats['obras_upd']+=cur.rowcount
    conn.commit()
    time.sleep(0.5)

# --- B) Role emails em domínios alternativos ---
ALT = [
    ('23972590000381', 'mahindrabrasil.com.br',  ['mahindra.com','auto.mahindra.com','mahindrarise.com.br'], 'Mahindra'),
    ('03007331000171', 'mercadolivre.com.br',     ['mercadolibre.com','meli.com','mercadolivre.com.ar'], 'Mercado Livre'),
    ('60409075000152', 'nestle.com.br',           ['br.nestle.com','nestle.com','br.nestlepurinaone.com'], 'Nestlé'),
    ('60476884000187', 'br.airliquide.com',       ['airliquide.com','airliquide.com.br'], 'Air Liquide (alt)'),
    ('38327308000119', 'pontesalvadoritaparica.com.br', ['ccrgrupo.com.br','grupoccr.com.br'], 'Ponte SI (alt CCR)'),
]
for cnpj, dom_atual, alts, label in ALT:
    print(f"\n--- ALT {label} ---")
    found=None
    for d in alts:
        print(f"  try {d}")
        r=find_role(d)
        if r:
            found=(r, d)
            break
    if not found:
        print(f"  NO_MATCH")
        continue
    (email, role, st, sc), dom = found
    cargo=role.title()
    stats['role_alt_found']+=1
    print(f"  ✓ {email} ({st})")
    cur.execute("""
        INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
            validacao_metodo, validacao_data)
        VALUES (%s,%s,%s,'manual_chat',4,'manual_chat_v9_7h',NOW())
        ON CONFLICT (cnpj) DO UPDATE SET dominio=EXCLUDED.dominio
    """, (cnpj, label, dom))
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_7h_alt_dom')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, f'Equipe {cargo}', cargo, sc, st))
    cur.execute("""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            decisor_status='DISCOVERED_V9_7H',
            nivel1_origem_enrichment='V9_7h_alt_dom'
        WHERE cnpj=%s AND classificacao_computed='PRATA'
          AND (nivel1_email_smtp_verified IS NOT TRUE OR nivel1_email_smtp_verified IS NULL)
    """, (email, sc, st, f'Equipe {cargo}', cargo, cnpj))
    n=cur.rowcount; stats['obras_upd']+=n
    print(f"  obras updated: {n}")
    conn.commit()

# --- C) Mega Condomínio clear bad email + retry ---
print(f"\n--- Mega Condominio Log: clear bad email ---")
cur.execute("""
    UPDATE obras SET nivel1_email=NULL, nivel1_email_smtp_verified=NULL,
        nivel1_email_status=NULL
    WHERE classificacao_computed='PRATA' AND empresa LIKE 'Mega Condom%'
      AND nivel1_email='sedec@serra.es.gov.br'
""")
print(f"  cleared: {cur.rowcount}")
conn.commit()

# --- D) Email Finder pros nomes reais em terminals/Moura ---
NAMED = [
    ('18091544000171', 'petrocitynavegacao.com.br', 'Jose', 'Silva',     'CPSM/Petrocity'),
    ('18648563000156', 'multicargas.com.br',         'Rossana','Cattalini','Novo Porto'),
    ('20391326000102', 'portocentral.com.br',        'Jose', 'Novaes',   'Porto Central'),
    ('24358329000197', 'evolveinfraestrutura.com.br','Delvan','Monteiro','Santos STS'),
    ('49695667000145', 'portodoitaqui.com.br',       'Gabriel','Cassia', 'Porto Itaqui'),
    ('03110981000118', 'ebt.com.br',                 'Aquiles','Teixeira','TUP EBT'),
    (None,             'cemig.com.br',               'Peterson','Giacomini','Consorcio Ita'),
    (None,             'morrodoipe.com.br',          'Andre',  'Puygcerver','Morro do Ipe'),
]
for cnpj, dom, first, last, label in NAMED:
    print(f"\n--- FINDER {label}: {first} {last} @ {dom} ---")
    d=finder(dom, deaccent(first), deaccent(last))
    email=d.get('email')
    score=d.get('score') or 0
    if not email or score<60:
        print(f"  no email (score={score})")
        time.sleep(1); continue
    v=verify(email)
    st=v.get('status') or 'unknown'
    smtp_ok=st in ('valid','accept_all','webmail') and (v.get('score') or 0)>=50
    print(f"  finder: {email} score={score} → verifier {st}")
    if smtp_ok: stats['finder_found']+=1
    where_clause = "cnpj=%s" if cnpj else "cnpj IS NULL AND empresa ILIKE %s"
    pattern = cnpj if cnpj else f"%{label.split('/')[0].split(' ')[0]}%"
    args = ([email, smtp_ok, score, st, cnpj] if cnpj else [email, smtp_ok, score, st, pattern])
    sql = f"""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=%s,
            nivel1_email_score=%s, nivel1_email_status=%s,
            decisor_status='DISCOVERED_V9_7H',
            nivel1_origem_enrichment='V9_7h_email_finder'
        WHERE classificacao_computed='PRATA' AND nivel1_email IS NULL AND {where_clause}
    """
    cur.execute(sql, args)
    n=cur.rowcount
    if smtp_ok: stats['obras_upd']+=n
    print(f"  obras updated: {n}")
    conn.commit()
    time.sleep(1.5)

# --- Mega Condominio: re-tentar role após clear ---
print(f"\n--- Mega Condominio Log: retry role marcosfarialog ---")
result=find_role('marcosfarialog.com.br')
if not result:
    result=find_role('mfl.com.br')
if result:
    email, role, st, sc = result
    cargo=role.title()
    cur.execute("""
        UPDATE obras SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_origem_enrichment='V9_7h_alt_dom'
        WHERE classificacao_computed='PRATA' AND empresa LIKE 'Mega Condom%'
    """, (email, sc, st))
    print(f"  ✓ {email}: obras updated={cur.rowcount}")
    if cur.rowcount: stats['obras_upd']+=cur.rowcount
    conn.commit()

print(f"\n=== STATS V9.7h ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
