"""V9.7e — Role emails brute-force nos 9 PRATA bucket 3 (sem email).
Estratégia: tentar compras/suprimentos/engenharia/contato/sac em cada dominio.
Mantém o nome do decisor existente, só adiciona email role-based.
"""
import os, time, requests, psycopg2

HUNTER_KEY=os.environ['HUNTER_API_KEY']

TARGETS = [
    ('03007331000171', 'mercadolivre.com.br',         'ML CD Logístico'),
    ('03110981000118', 'ebt.com.br',                  'TUP EBT'),
    ('13970936000197', 'tepor.com.br',                'TEPOR Macaé'),
    ('18091544000171', 'petrocitynavegacao.com.br',   'CPSM Petrocity'),
    ('18648563000156', 'multicargas.com.br',          'Novo Porto Multicargas'),
    ('20391326000102', 'portocentral.com.br',         'Porto Central'),
    ('24358329000197', 'evolveinfraestrutura.com.br', 'Santos STS'),
    ('49695667000145', 'portodoitaqui.com.br',        'Porto Itaqui EMAP'),
    ('60409075000152', 'nestle.com.br',               'Nestlé'),
]

ROLES = ['compras','suprimentos','procurement','engenharia','projetos','manutencao',
         'operacoes','obras','contato','sac','licitacao','sourcing']
CARGO_LABELS = {
    'compras':'Compras','suprimentos':'Suprimentos','procurement':'Procurement',
    'engenharia':'Engenharia','projetos':'Projetos','manutencao':'Manutenção',
    'operacoes':'Operações','obras':'Obras','contato':'Contato',
    'sac':'SAC','licitacao':'Licitação','sourcing':'Sourcing'
}

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'verified':0,'found':0,'obras_upd':0}

for cnpj, dom, label in TARGETS:
    print(f"\n=== {label} @ {dom} ===")
    found=None
    for role in ROLES:
        email=f"{role}@{dom}"
        stats['verified']+=1
        d=verify(email)
        status=d.get('status') or 'unknown'
        score=d.get('score') or 0
        if status in ('valid','accept_all','webmail') and score>=50:
            print(f"  ✓ {email} → {status} {score}")
            found=(email, role, status, score)
            break
        else:
            print(f"  ✗ {email} → {status}")
        time.sleep(0.8)
    if not found:
        print(f"  NO ROLE EMAIL VALID")
        continue
    email, role, status, score = found
    cargo=CARGO_LABELS.get(role, role.title())
    stats['found']+=1
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_7e_role_bucket3')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, f'Equipe {cargo}', cargo, score, status))
    # NÃO sobrescrever nome do decisor existente; só adicionar email.
    cur.execute("""
        UPDATE obras
        SET nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            decisor_status='DISCOVERED_V9_7E',
            nivel1_origem_enrichment='V9_7e_role_bucket3'
        WHERE cnpj=%s AND classificacao_computed='PRATA'
          AND nivel1_email IS NULL
    """, (email, score, status, cnpj))
    n=cur.rowcount; stats['obras_upd']+=n
    print(f"  obras updated: {n}")
    conn.commit()

print(f"\n=== STATS V9.7e ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
