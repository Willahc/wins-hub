"""V9.9 T2 — Email Finder + fallback + Verifier nos 4 PRATA last mile."""
import os, time, requests, psycopg2
from unicodedata import normalize

HUNTER_KEY=os.environ['HUNTER_API_KEY']

def deaccent(s): return normalize('NFKD',s).encode('ascii','ignore').decode('ascii')

def finder(domain, first, last):
    try:
        r=requests.get("https://api.hunter.io/v2/email-finder",
            params={'domain':domain,'first_name':first,'last_name':last,
                    'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except Exception as e:
        print(f"  ERR finder: {e}", flush=True)
        return {}

def verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except Exception as e:
        print(f"  ERR verify: {e}", flush=True)
        return {}

# (nome_completo, domain, fallback_email, cargo_hint, empresa_pattern)
TARGETS=[
    ('Clarisse Etcheverry',       'glp.com',                  'clarisse.etcheverry@glp.com',           'Diretora Novos Projetos GLP',           '%Mega Condom%'),
    ('André Luiz Puygcerver',     'ipemineracao.com',         'andre.puygcerver@ipemineracao.com',     'Gerente Operação Mineração Morro do Ipê', '%Morro do Ip%'),
    ('Gustavo Emina',             'newwavegroup.com',         'gustavo.emina@newwavegroup.com',        'Diretor New Wave Tech',                 '%New Wave%'),
    ('Rossana Cattalini',         'cattaliniterminais.com.br','rossana.cattalini@cattaliniterminais.com.br','Sócia Novo Porto Terminais TPML', '%Novo Porto%'),
]

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'finder_hit':0,'fallback':0,'verified_valid':0,'obras_upd':0}

for full_name, domain, fallback_email, cargo, pat in TARGETS:
    print(f"\n=== {full_name} @ {domain} ===", flush=True)
    parts=full_name.split()
    parts=[p for p in parts if p.lower() not in ('da','de','do','dos','das','e','&','-')]
    first=deaccent(parts[0])
    last=deaccent(parts[-1])

    # Step 1: Hunter Email Finder
    d=finder(domain, first, last)
    email=d.get('email')
    score=d.get('score') or 0
    linkedin=d.get('linkedin_url')
    print(f"  finder: email={email} score={score}", flush=True)

    used_email = None
    used_score = 0
    used_source = None

    if email and score >= 40:
        used_email = email
        used_score = score
        used_source = 'finder'
        stats['finder_hit'] += 1
    else:
        used_email = fallback_email
        used_source = 'fallback_pattern'
        stats['fallback'] += 1
        print(f"  → fallback: {fallback_email}", flush=True)

    # Step 2: Verify
    time.sleep(1)
    v = verify(used_email)
    status = v.get('status') or 'unknown'
    vscore = v.get('score') or 0
    print(f"  verify: status={status} score={vscore}", flush=True)
    smtp_ok = status in ('valid','accept_all','webmail') and vscore >= 50
    if not smtp_ok and status == 'unknown' and vscore >= 70:
        smtp_ok = True
        status = 'unknown_accept'
    if smtp_ok:
        stats['verified_valid'] += 1

    # Update empresa_dominios (case: GLP domain may not be mapped yet for Mega Condominio)
    if 'Mega' in pat:
        # Set CNPJ canonico for Mega Condominio (use placeholder if existing)
        cur.execute("SELECT cnpj FROM obras WHERE classificacao_computed='PRATA' AND empresa ILIKE %s LIMIT 1", (pat,))
        row = cur.fetchone()
        cnpj_atual = row[0] if row else None
        if not cnpj_atual:
            # Use GLP Brasil CNPJ canonico
            cnpj_glp = '11231630000186'  # GLP Brasil Empreendimentos
            cur.execute("UPDATE obras SET cnpj=%s WHERE classificacao_computed='PRATA' AND empresa ILIKE %s AND cnpj IS NULL", (cnpj_glp, pat))
            cur.execute("""
                INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca, validacao_metodo, validacao_data)
                VALUES (%s,'GLP Brasil',%s,'manual_chat',4,'manual_chat_v9_last_mile',NOW())
                ON CONFLICT (cnpj) DO UPDATE SET dominio=EXCLUDED.dominio
            """, (cnpj_glp, domain))
    elif 'Novo Porto' in pat:
        # Mapping update Novo Porto -> cattaliniterminais.com.br
        cur.execute("""
            UPDATE empresa_dominios SET dominio=%s, validacao_metodo='manual_chat_v9_last_mile', validacao_data=NOW()
            WHERE cnpj='18648563000156'
        """, (domain,))

    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, hunter_score, hunter_status, origem, linkedin_url)
        SELECT cnpj, %s, %s, %s, %s, %s, %s, 'V9_last_mile_manual', %s
        FROM obras
        WHERE classificacao_computed='PRATA' AND empresa ILIKE %s
        LIMIT 1
        ON CONFLICT (email) DO NOTHING
    """, (domain, used_email, full_name, cargo, used_score, status, linkedin, pat))

    cur.execute("""
        UPDATE obras
        SET nivel1_nome = %s,
            nivel1_cargo = %s,
            nivel1_email = %s,
            nivel1_email_smtp_verified = %s,
            nivel1_email_score = %s,
            nivel1_email_status = %s,
            nivel1_linkedin = COALESCE(%s, nivel1_linkedin),
            decisor_status = 'DISCOVERED_V9_LAST_MILE',
            nivel1_origem_enrichment = 'V9_last_mile_manual'
        WHERE classificacao_computed = 'PRATA' AND empresa ILIKE %s
          AND (nivel1_email_smtp_verified IS NULL OR nivel1_email_smtp_verified = FALSE)
    """, (full_name, cargo, used_email, smtp_ok, used_score, status, linkedin, pat))
    n = cur.rowcount
    stats['obras_upd'] += n
    print(f"  obras updated: {n} (source={used_source}, smtp_ok={smtp_ok})", flush=True)
    conn.commit()
    time.sleep(1)

print(f"\n=== STATS V9.9 ===", flush=True)
for k,v in stats.items(): print(f"  {k}: {v}", flush=True)
cur.close(); conn.close()
