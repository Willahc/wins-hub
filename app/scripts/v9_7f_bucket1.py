"""V9.7f — Bucket 1 (sem dominio) + Cesan especial (cnpj NULL).
- 7 CNPJs com 1 obra cada: mapear domínio + role email
- 27 obras Cesan (cnpj NULL): mapear via empresa LIKE, usar domínio Cesan
"""
import os, time, requests, psycopg2

HUNTER_KEY=os.environ['HUNTER_API_KEY']

# CNPJ → (nome, domínio candidato)
KNOWN = [
    ('03928294000104', 'ITF Chemical',          'itfchemical.com.br'),
    ('05571228001550', 'Fertilizantes Tocantins','fertilizantestocantins.com.br'),
    ('05740526000121', 'Linde',                  'linde.com'),  # Linde global
    ('02092515000437', 'Mare Fertilizantes',     'marefertilizantes.com.br'),
    ('33958695001140', 'Unipar Carbocloro',      'unipar.ind.br'),
    ('43843358001322', 'Air Products',           'airproducts.com'),  # global
    ('33657248000189', 'BNDES Ind ES',           'bndes.gov.br'),
]
# Cesan: CNPJ real é 28.151.363/0001-47 = 28151363000147
CESAN_CNPJ = '28151363000147'
CESAN_NAME = 'CESAN - Companhia Espírito Santense de Saneamento'
CESAN_DOMAIN = 'cesan.com.br'

ROLES = ['compras','suprimentos','procurement','engenharia','projetos','manutencao',
         'operacoes','obras','contato','sac','licitacao','sourcing','cgcl','licitacoes']
CARGOS = {'compras':'Compras','suprimentos':'Suprimentos','procurement':'Procurement',
    'engenharia':'Engenharia','projetos':'Projetos','manutencao':'Manutenção',
    'operacoes':'Operações','obras':'Obras','contato':'Contato','sac':'SAC',
    'licitacao':'Licitação','licitacoes':'Licitações','sourcing':'Sourcing',
    'cgcl':'Cadastro Geral Compras Licitações'}

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

def find_role(domain):
    for role in ROLES:
        email=f"{role}@{domain}"
        d=verify(email)
        status=d.get('status') or 'unknown'
        score=d.get('score') or 0
        print(f"   {email} → {status} {score}")
        time.sleep(0.8)
        if status in ('valid','accept_all','webmail') and score>=50:
            return email, role, status, score
    return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'mapped':0,'found':0,'obras_upd':0}

def map_and_email(cnpj, name, domain):
    print(f"\n=== {name} ({cnpj}) → {domain} ===")
    cur.execute("""
        INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
            validacao_metodo, validacao_data)
        VALUES (%s,%s,%s,'manual_chat',4,'manual_chat_v9_7f',NOW())
        ON CONFLICT (cnpj) DO UPDATE
          SET dominio=EXCLUDED.dominio, validacao_metodo='manual_chat_v9_7f',
              validacao_data=NOW()
    """, (cnpj, name, domain))
    stats['mapped']+=1
    result=find_role(domain)
    if not result:
        print(f"  NO ROLE EMAIL")
        return
    email, role, status, score = result
    cargo=CARGOS.get(role, role.title())
    stats['found']+=1
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_7f_bucket1')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, domain, email, f'Equipe {cargo}', cargo, score, status))
    cur.execute("""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            decisor_status='DISCOVERED_V9_7F',
            nivel1_origem_enrichment='V9_7f_bucket1'
        WHERE cnpj=%s AND classificacao_computed='PRATA'
    """, (email, score, status, f'Equipe {cargo}', cargo, cnpj))
    n=cur.rowcount; stats['obras_upd']+=n
    print(f"  ✓ {email} → obras updated: {n}")
    conn.commit()

for cnpj, name, dom in KNOWN:
    map_and_email(cnpj, name, dom)

# CESAN — 27 obras com cnpj NULL
print(f"\n=== CESAN especial (cnpj NULL → {CESAN_CNPJ}) ===")
cur.execute("""
    INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
        validacao_metodo, validacao_data)
    VALUES (%s,%s,%s,'manual_chat',5,'manual_chat_v9_7f',NOW())
    ON CONFLICT (cnpj) DO UPDATE
      SET dominio=EXCLUDED.dominio, validacao_metodo='manual_chat_v9_7f',
          validacao_data=NOW()
""", (CESAN_CNPJ, CESAN_NAME, CESAN_DOMAIN))
result=find_role(CESAN_DOMAIN)
if result:
    email, role, status, score = result
    cargo=CARGOS.get(role, role.title())
    stats['found']+=1
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_7f_bucket1_cesan')
        ON CONFLICT (email) DO NOTHING
    """, (CESAN_CNPJ, CESAN_DOMAIN, email, f'Equipe {cargo}', cargo, score, status))
    cur.execute("""
        UPDATE obras
        SET cnpj=%s, nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            decisor_status='DISCOVERED_V9_7F',
            nivel1_origem_enrichment='V9_7f_bucket1_cesan'
        WHERE classificacao_computed='PRATA' AND cnpj IS NULL AND empresa ILIKE '%%Cesan%%'
    """, (CESAN_CNPJ, email, score, status, f'Equipe {cargo}', cargo))
    n=cur.rowcount; stats['obras_upd']+=n
    print(f"  ✓ {email} → cesan obras updated: {n}")
    conn.commit()
else:
    print(f"  Cesan: NO ROLE EMAIL")

print(f"\n=== STATS V9.7f ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
