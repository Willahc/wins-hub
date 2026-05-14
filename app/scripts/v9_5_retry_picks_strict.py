"""V9.5 — re-search nos 6 CNPJs com pick fraco V9.4.

Filtro endurecido:
- EXCLUDE: treasury, risk, safety, chartering, BD, locomotive, marketing, comercial,
           vendas, sales, revenue, communications, investor, audit, compliance,
           juridico, legal, counsel
- REQUIRED: P1 (suprimentos/compras/procurement/supply chain/sourcing)
            OR P2 (engenharia/projetos/manutencao/obras/construcao)
- score >= 70
- dedup contra contatos_alternativos.email
- Se NO_MATCH: mantém decisor atual.
"""
import os, time, requests, psycopg2, re

HUNTER_KEY = os.environ['HUNTER_API_KEY']

P1 = re.compile(
    r'\b(suprimentos?|compras|procurement|supply[\s-]?chain|sourcing|aquisi[cç][aã]o|'
    r'purchas\w*)\b', re.I)
P2 = re.compile(
    r'\b(engenharia|engineer\w*|projetos?|projects?|manuten[cç][aã]o|maintenance|'
    r'implanta[cç][aã]o|obras\b|constru[cç][aã]o|construction|epc\b|civil\b)\b', re.I)
P3 = re.compile(  # modifier — só conta se já houver P1/P2
    r'\b(gerente|manager|coordenador|coordinator|supervisor|head|director|diretor)\b', re.I)

EXCLUDE = re.compile(
    r'\b(treasur\w*|risk\b|safety\b|seguran[cç]a|chartering|frete\w*|fretamento|'
    r'business[\s-]?develop\w*|\bbd\b|locomotive\w*|maquinista|conductor|motorista|'
    r'audit\w*|compliance|jur[ií]dic\w*|legal\b|counsel|advogad\w*|'
    r'investor|rela[cç][aã]o[\s-]?\w*investid\w*|communicat\w*|comunic\w*|'
    r'marketing|comercial\b|vendas\b|sales\b|revenue|press|imprensa|'
    r'rh\b|recursos[\s-]?humanos|human[\s-]?resources|people\b|cultura\b|'
    r'finan[cç]\w*|finance|cfo\b|cto\b|coo\b|cmo\b|ceo\b|'
    r'presidente|vice[\s-]?presidente|vp\b|sustainab\w*|sustentab\w*|esg\b|'
    r'ti\b|tecnologia[\s-]?da[\s-]?inf\w*|it[\s-]?\w*|data\b|analytics)\b', re.I)

TARGETS = [
    ('17234244000131', 'csn.com.br',              'Transnordestina/FTL'),
    ('42150391000170', 'vli-logistica.com.br',    'VLI Multimodal'),
    ('19726111000108', 'riogaleao.com',           'RioGaleão'),
    ('02502844000166', 'rumolog.com',             'Rumo Malha Paulista'),
    ('33000167000101', 'petrobras.com.br',        'Petrobras'),
    ('23834518000126', 'cedromineracao.com.br',   'Cedro Mineração'),
]

def hunter_domain_search(domain):
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search",
            params={'domain': domain, 'api_key': HUNTER_KEY,
                    'limit': 10, 'type': 'personal'}, timeout=30)
        return r.json().get('data', {}).get('emails', [])
    except Exception as e:
        print(f"  ERR hunter {domain}: {e}")
        return []

def cargo_match(cargo):
    """Retorna (tier, ok). tier=1 P1, 2 P2; ok=True se passa filtro."""
    if not cargo:
        return None, False
    if EXCLUDE.search(cargo):
        return None, False
    if P1.search(cargo):
        return 1, True
    if P2.search(cargo):
        return 2, True
    return None, False

def pick_best(emails, skip_emails):
    candidates = []
    for em in emails:
        email = (em.get('value') or '').lower()
        if not email or email in skip_emails:
            continue
        score = em.get('confidence') or 0
        if score < 70:
            continue
        cargo = em.get('position') or ''
        tier, ok = cargo_match(cargo)
        if not ok:
            continue
        candidates.append((tier, -score, email, em))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][3]

conn = psycopg2.connect(
    host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'])
cur = conn.cursor()

stats = {'searched': 0, 'replaced': 0, 'no_match': 0, 'inserted': 0, 'obras_updated': 0}

for cnpj, dominio, label in TARGETS:
    print(f"\n--- {label} ({cnpj}) @ {dominio} ---")
    stats['searched'] += 1

    cur.execute("""
        SELECT LOWER(email) FROM contatos_alternativos
        WHERE cnpj=%s OR empresa_dominio=%s
    """, (cnpj, dominio))
    skip = {row[0] for row in cur.fetchall() if row[0]}
    cur.execute("SELECT DISTINCT LOWER(nivel1_email) FROM obras WHERE cnpj=%s AND nivel1_email IS NOT NULL", (cnpj,))
    skip |= {row[0] for row in cur.fetchall() if row[0]}
    print(f"  skip emails: {len(skip)}")

    emails = hunter_domain_search(dominio)
    print(f"  hunter returned {len(emails)} emails")

    best = pick_best(emails, skip)
    if not best:
        stats['no_match'] += 1
        print(f"  NO_MATCH (P1/P2 + score>=70 + ! EXCLUDE)")
        time.sleep(2)
        continue

    nome = ((best.get('first_name') or '') + ' ' + (best.get('last_name') or '')).strip()
    cargo = best.get('position') or ''
    email = best.get('value')
    score = best.get('confidence') or 0
    linkedin = best.get('linkedin') or None
    verif_status = (best.get('verification') or {}).get('status') or 'unknown'
    smtp_ok = (verif_status == 'valid')
    departamento = best.get('department') or ''

    print(f"  REPLACE: {nome} | {cargo} | {email} | score={score} | smtp={verif_status}")
    stats['replaced'] += 1

    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, departamento, linkedin_url,
           hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'V9_5_retry_strict')
        ON CONFLICT (email) DO NOTHING RETURNING id
    """, (cnpj, dominio, email, nome, cargo, departamento, linkedin, score, verif_status))
    if cur.fetchone():
        stats['inserted'] += 1

    cur.execute("""
        UPDATE obras
        SET nivel1_nome = %s,
            nivel1_cargo = %s,
            nivel1_email = %s,
            nivel1_email_smtp_verified = %s,
            nivel1_email_score = %s,
            nivel1_email_status = %s,
            decisor_status = 'DISCOVERED_V9_5_STRICT',
            nivel1_origem_enrichment = 'V9_5_retry_strict'
        WHERE cnpj = %s
          AND classificacao_computed = 'PRATA'
          AND nivel1_origem_enrichment = 'V9_prata_domain_search'
    """, (nome, cargo, email, smtp_ok, score, verif_status, cnpj))
    n = cur.rowcount
    stats['obras_updated'] += n
    print(f"  obras updated: {n}")
    conn.commit()
    time.sleep(2)

print(f"\n=== STATS ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
cur.close()
conn.close()
