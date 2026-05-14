"""V9.7c — Email Verifier em role emails comuns nos 3 dominios sem cobertura Hunter:
LOG-IN, Enerpeixe, AXIA. Também tenta parent domains."""
import os, time, requests, psycopg2

HUNTER_KEY = os.environ['HUNTER_API_KEY']

CANDIDATES = [
    ('42278291000124', 'LOG-IN', ['loginlogistica.com.br'], [
        'compras','suprimentos','procurement','engenharia','projetos','manutencao',
        'operacoes','obras','contato','atendimento','sourcing','licitacao']),
    ('03983431000103', 'Enerpeixe', ['enerpeixe.com.br','edpbr.com.br','edp.com.br'], [
        'compras','suprimentos','procurement','engenharia','projetos','manutencao',
        'operacoes','obras','contato','sourcing','licitacao']),
    ('33541368000116', 'AXIA Energia', ['axia.com.br','chesf.com.br'], [
        'compras','suprimentos','procurement','engenharia','projetos','manutencao',
        'operacoes','obras','contato','sourcing','licitacao']),
]

ROLE_TO_CARGO = {
    'compras': 'Compras',
    'suprimentos': 'Suprimentos',
    'procurement': 'Procurement',
    'engenharia': 'Engenharia',
    'projetos': 'Projetos',
    'manutencao': 'Manutenção',
    'operacoes': 'Operações',
    'obras': 'Obras',
    'sourcing': 'Sourcing',
    'licitacao': 'Licitação',
    'contato': 'Contato Geral',
    'atendimento': 'Atendimento',
}

def hunter_verify(email):
    try:
        r=requests.get("https://api.hunter.io/v2/email-verifier",
            params={'email':email,'api_key':HUNTER_KEY}, timeout=30)
        return r.json().get('data',{})
    except: return {}

conn=psycopg2.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],dbname=os.environ['DB_NAME'])
cur=conn.cursor()

stats={'verified':0,'found':0,'obras_updated':0}

for cnpj, label, domains, roles in CANDIDATES:
    print(f"\n=== {label} ({cnpj}) ===")
    found = None
    for dom in domains:
        if found: break
        for role in roles:
            email = f"{role}@{dom}"
            stats['verified']+=1
            d = hunter_verify(email)
            status = d.get('status') or 'unknown'
            score = d.get('score') or 0
            print(f"  {email} → status={status} score={score}")
            time.sleep(1)
            if status in ('valid','accept_all','webmail') and score >= 50:
                found = (email, role, dom, status, score)
                break
    if not found:
        print(f"  NO ROLE EMAIL VALIDATED")
        continue
    email, role, dom, status, score = found
    cargo = ROLE_TO_CARGO.get(role, role.title())
    nome = f"Equipe {cargo}"
    print(f"  ✓ FOUND: {email} | {cargo} | status={status}")
    stats['found']+=1

    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, departamento, hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'V9_7c_role_email')
        ON CONFLICT (email) DO NOTHING
    """, (cnpj, dom, email, nome, cargo, cargo, score, status))

    cur.execute("""
        UPDATE obras
        SET nivel1_nome=%s, nivel1_cargo=%s, nivel1_email=%s,
            nivel1_email_smtp_verified=TRUE, nivel1_email_score=%s,
            nivel1_email_status=%s, decisor_status='DISCOVERED_V9_7C',
            nivel1_origem_enrichment='V9_7c_role_email'
        WHERE cnpj=%s
          AND classificacao_computed='PRATA'
          AND (nivel1_email IS NULL OR nivel1_origem_enrichment='V9_7_cleared_remap')
    """, (nome, cargo, email, score, status, cnpj))
    n=cur.rowcount
    stats['obras_updated']+=n
    print(f"  obras updated: {n}")
    conn.commit()

print(f"\n=== STATS V9.7c ===")
for k,v in stats.items(): print(f"  {k}: {v}")
cur.close(); conn.close()
