"""V9.7g — Para 25 empresas PRATA com cnpj NULL: setar CNPJ + domínio + role email."""
import os, time, requests, psycopg2

HUNTER_KEY=os.environ['HUNTER_API_KEY']

# (empresa_pattern, cnpj_canonico, domain_candidates, label)
MAPPINGS = [
    ('%SAMARCO%',           '16628281000161', ['samarco.com'],                  'Samarco Mineração'),
    ('%Baterias Moura%',    '01124883000156', ['moura.com.br'],                 'Baterias Moura'),
    ('%Eurofarma%',         '61190096000192', ['eurofarma.com.br'],             'Eurofarma'),
    ('%Cofco%',             '07043999000110', ['cofcointernational.com','cofco.com'], 'Cofco'),
    ('%Daikin Brasil%',     '47619135000131', ['daikin.com.br'],                'Daikin Brasil'),
    ('%Grendene%',          '89850341000160', ['grendene.com.br'],              'Grendene'),
    ('%GAC Motors%',        '37335515000139', ['gacmotor.com.br','gac.com.br'], 'GAC Motors'),
    ('%General Motors%',    '59275792000150', ['gm.com.br','gm.com'],           'General Motors Brasil'),
    ('%Jefer%',             '02916265000160', ['jefer.com.br'],                 'Grupo Jefer'),
    ('%MAR AZUL%',          '06119930000180', ['marazullog.com.br','marazul.com.br'], 'Mar Azul'),
    ('%MORRO DO IPÊ%',      '17249007000139', ['morrodoipe.com.br'],            'Mineracao Morro do Ipe'),
    ('Mega Condomínio Log.','61156763000133', ['marcosfarialog.com.br'],        'Mega Condominio Log'),
    ('%Mina de Ferro (Samarco)%','16628281000161', ['samarco.com'],            'Samarco Mineração'),
    ('Nova Linha de Motores (Toyota)','59104760000119',['toyota.com.br'],      'Toyota'),
    ('%POTÁSSIO DO BRASIL%','09262924000130', ['potassiodobrasil.com.br','mosaicco.com.br'], 'Potassio do Brasil (Mosaic)'),
    ('%Panfácil%',          '54870208000189', ['panfacil.com.br'],              'Panfacil'),
    ('%BAHER%',             '13769320000168', ['baher.com.br','heringer.com.br'], 'BAHER (Heringer)'),
    ('%Schomäcker%',        '79924770000124', ['schomacker.com.br'],            'Schomacker'),
    ('%CORUMBÁ%',           '01643923000147', ['cemig.com.br'],                 'Corumba Concessoes (Cemig)'),
    ('%CAPIM BRANCO%',      '17155730000164', ['cemig.com.br'],                 'Capim Branco (Cemig)'),
    ('%IGARAPAVA%',         '02998337000175', ['cemig.com.br'],                 'Igarapava (Cemig)'),
    ('%MACHADINHO%',        '01376316000196', ['celesc.com.br'],                'Machadinho'),
    ('%CONSÓRCIO ITÁ%',     '01437038000142', ['tractebel.com.br','engie.com.br'], 'Consorcio Ita (Engie)'),
]

ROLES = ['compras','suprimentos','procurement','engenharia','projetos','manutencao',
         'operacoes','obras','contato','sourcing','licitacoes','licitacao']

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
        time.sleep(0.7)
        if status in ('valid','accept_all','webmail') and score>=50:
            return email, role, status, score
    return None

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'tried':0,'found':0,'obras_upd':0,'no_match_empresas':[]}

for pattern, cnpj, domains, label in MAPPINGS:
    print(f"\n=== {label} | pattern={pattern} | cnpj={cnpj} ===")
    stats['tried']+=1
    cur.execute("SELECT COUNT(*) FROM obras WHERE classificacao_computed='PRATA' AND cnpj IS NULL AND empresa ILIKE %s", (pattern,))
    cnt=cur.fetchone()[0]
    if cnt==0:
        print(f"  no obras match pattern")
        continue
    print(f"  obras matching: {cnt}")

    found=None
    for dom in domains:
        print(f"  try {dom}")
        result=find_role(dom)
        if result:
            found=(result, dom)
            break
    if not found:
        stats['no_match_empresas'].append(label)
        print(f"  NO_ROLE_FOUND")
        continue
    (email, role, status, score), dom = found
    cargo=role.title()
    stats['found']+=1
    print(f"  ✓ {email} ({status})")

    cur.execute("""
        INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca,
            validacao_metodo, validacao_data)
        VALUES (%s,%s,%s,'manual_chat',4,'manual_chat_v9_7g',NOW())
        ON CONFLICT (cnpj) DO UPDATE SET dominio=EXCLUDED.dominio, validacao_data=NOW()
    """, (cnpj, label, dom))
    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'V9_7g_null_cnpj')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, f'Equipe {cargo}', cargo, score, status))
    cur.execute("""
        UPDATE obras
        SET cnpj=%s,
            nivel1_email=%s, nivel1_email_smtp_verified=TRUE,
            nivel1_email_score=%s, nivel1_email_status=%s,
            nivel1_nome=COALESCE(NULLIF(nivel1_nome,''), %s),
            nivel1_cargo=COALESCE(NULLIF(nivel1_cargo,''), %s),
            decisor_status='DISCOVERED_V9_7G',
            nivel1_origem_enrichment='V9_7g_null_cnpj'
        WHERE classificacao_computed='PRATA' AND cnpj IS NULL AND empresa ILIKE %s
    """, (cnpj, email, score, status, f'Equipe {cargo}', cargo, pattern))
    n=cur.rowcount; stats['obras_upd']+=n
    print(f"  obras updated: {n}")
    conn.commit()

print(f"\n=== STATS V9.7g ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
