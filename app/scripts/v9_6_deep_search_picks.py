"""V9.6 — Deep search nos 5 NO_MATCH persistentes do V9.5.

Estratégias por CNPJ (ordem):
A) Hunter Domain Search type=personal, seniority=senior,executive, limit=100
B) Serper LinkedIn search por cargo + Hunter Email Finder
C) Hunter Domain Search type=generic (compras@, suprimentos@, engenharia@)
"""
import os, time, requests, psycopg2, re

HUNTER_KEY = os.environ['HUNTER_API_KEY']
SERPER_KEY = os.environ['SERPER_API_KEY']

P1 = re.compile(
    r'\b(suprimentos?|compras|procurement|supply[\s-]?chain|sourcing|aquisi[cç][aã]o|'
    r'purchas\w*|buyer|comprador)\b', re.I)
P2 = re.compile(
    r'\b(engenharia|engineer\w*|projetos?|projects?|manuten[cç][aã]o|maintenance|'
    r'implanta[cç][aã]o|obras\b|constru[cç][aã]o|construction|epc\b|civil\b|'
    r'opera[cç][oõ]es|operations\b|industrial|planta\b|plant[\s-]?manager|'
    r'fabril|f[aá]brica|infraestrutura|infrastructure|t[eé]cnico|t[eé]cnica|technical|'
    r'facility|facilities|asset\s+manag|ativos\b)\b', re.I)

EXCLUDE = re.compile(
    r'\b(treasur\w*|risk\b|safety\b|seguran[cç]a|chartering|frete\w*|fretamento|'
    r'business[\s-]?develop\w*|\bbd\b|locomotive\w*|maquinista|conductor|motorista|'
    r'audit\w*|compliance|jur[ií]dic\w*|legal\b|counsel|advogad\w*|'
    r'investor|rela[cç][aã]o[\s-]?\w*investid\w*|communicat\w*|comunic\w*|'
    r'marketing|comercial\b|vendas\b|sales\b|revenue|press|imprensa|midia|'
    r'rh\b|recursos[\s-]?humanos|human[\s-]?resources|people\b|cultura\b|'
    r'finan[cç]\w*|finance|cfo\b|cto\b|coo\b|cmo\b|ceo\b|'
    r'presidente|vice[\s-]?presidente|vp\b|sustainab\w*|sustentab\w*|esg\b)\b', re.I)

TARGETS = [
    ('17234244000131', 'csn.com.br',              'Transnordestina/FTL (CSN)', 'CSN'),
    ('19726111000108', 'riogaleao.com',           'RioGaleão',                  'RioGaleao'),
    ('02502844000166', 'rumolog.com',             'Rumo Malha Paulista',        'Rumo'),
    ('33000167000101', 'petrobras.com.br',        'Petrobras',                  'Petrobras'),
    ('23834518000126', 'cedromineracao.com.br',   'Cedro Mineração',            'Cedro Mineracao'),
]

def cargo_ok(cargo):
    if not cargo:
        return None
    if EXCLUDE.search(cargo):
        return None
    if P1.search(cargo):
        return 1
    if P2.search(cargo):
        return 2
    return None

def pick_best(emails, skip_emails):
    cands = []
    for em in emails:
        email = (em.get('value') or '').lower()
        if not email or email in skip_emails:
            continue
        score = em.get('confidence') or 0
        if score < 70:
            continue
        cargo = em.get('position') or ''
        tier = cargo_ok(cargo)
        if not tier:
            continue
        cands.append((tier, -score, email, em))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    return cands[0][3]

def hunter_domain_search(domain, limit=100, type_='personal', seniority=None):
    params = {'domain': domain, 'api_key': HUNTER_KEY, 'limit': limit, 'type': type_}
    if seniority:
        params['seniority'] = seniority
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search", params=params, timeout=30)
        j = r.json()
        if 'errors' in j:
            print(f"  hunter errors: {j['errors']}")
            return []
        return j.get('data', {}).get('emails', [])
    except Exception as e:
        print(f"  ERR domain-search: {e}")
        return []

def hunter_email_finder(domain, first, last):
    try:
        r = requests.get("https://api.hunter.io/v2/email-finder",
            params={'domain': domain, 'first_name': first, 'last_name': last,
                    'api_key': HUNTER_KEY}, timeout=30)
        j = r.json()
        if 'errors' in j:
            return None
        d = j.get('data', {})
        if not d.get('email'):
            return None
        return {
            'value': d['email'],
            'first_name': d.get('first_name') or first,
            'last_name': d.get('last_name') or last,
            'position': d.get('position') or '',
            'department': d.get('department') or '',
            'linkedin': d.get('linkedin_url') or None,
            'confidence': d.get('score') or 0,
            'verification': {'status': (d.get('verification') or {}).get('status') or 'unknown'},
        }
    except Exception as e:
        print(f"  ERR email-finder: {e}")
        return None

def serper_linkedin(empresa, role_terms):
    q = f'"{empresa}" ({role_terms}) site:linkedin.com/in/'
    try:
        r = requests.post('https://google.serper.dev/search',
            headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'},
            json={'q': q, 'hl': 'pt', 'gl': 'br', 'num': 10}, timeout=15)
        return r.json().get('organic', [])
    except Exception as e:
        print(f"  ERR serper: {e}")
        return []

NAME_STOP = {'mr','mrs','ms','dr','sr','sra','eng','engº','engo','prof','dra','phd'}

def parse_linkedin_result(item):
    """Extract (first, last, cargo_snippet) from a Serper organic result."""
    title = item.get('title') or ''
    snippet = item.get('snippet') or ''
    # Title usually: "Joao Silva - Gerente de Compras - Petrobras | LinkedIn"
    t = re.sub(r'\s*[\|·]\s*linkedin.*$', '', title, flags=re.I)
    parts = re.split(r'\s+[-–—]\s+', t)
    if len(parts) < 2:
        return None
    name_raw = parts[0].strip()
    cargo_raw = parts[1].strip()
    name_clean = re.sub(r'[^\w\sÀ-ÿ.\'-]', '', name_raw).strip()
    tokens = [t for t in name_clean.split() if t.lower() not in NAME_STOP]
    if len(tokens) < 2 or len(tokens) > 6:
        return None
    first = tokens[0]
    last = tokens[-1]
    if first.isupper() and len(first) > 5:
        return None
    # Skip if 'last' is just a single letter
    if len(last) < 2:
        return None
    return first, last, cargo_raw

conn = psycopg2.connect(
    host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'])
cur = conn.cursor()

stats = {'searched': 0, 'replaced': 0, 'no_match': 0, 'inserted': 0,
         'obras_updated': 0, 'hunter_calls': 0, 'serper_calls': 0}

def apply_pick(cnpj, dominio, label, best, source):
    nome = ((best.get('first_name') or '') + ' ' + (best.get('last_name') or '')).strip()
    cargo = best.get('position') or ''
    email = best.get('value')
    score = best.get('confidence') or 0
    linkedin = best.get('linkedin') or None
    verif_status = (best.get('verification') or {}).get('status') or 'unknown'
    smtp_ok = (verif_status == 'valid')
    departamento = best.get('department') or ''
    origem_tag = f'V9_6_{source}'

    print(f"  REPLACE ({source}): {nome} | {cargo} | {email} | score={score} | smtp={verif_status}")
    stats['replaced'] += 1

    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, departamento, linkedin_url,
           hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (email) DO NOTHING RETURNING id
    """, (cnpj, dominio, email, nome, cargo, departamento, linkedin, score, verif_status, origem_tag))
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
            decisor_status = 'DISCOVERED_V9_6',
            nivel1_origem_enrichment = %s,
            nivel1_linkedin = COALESCE(%s, nivel1_linkedin)
        WHERE cnpj = %s
          AND classificacao_computed = 'PRATA'
          AND nivel1_origem_enrichment IN ('V9_prata_domain_search','V9_5_retry_strict')
    """, (nome, cargo, email, smtp_ok, score, verif_status, origem_tag, linkedin, cnpj))
    n = cur.rowcount
    stats['obras_updated'] += n
    print(f"  obras updated: {n}")
    conn.commit()

for cnpj, dominio, label, empresa_simple in TARGETS:
    print(f"\n=== {label} ({cnpj}) @ {dominio} ===")
    stats['searched'] += 1

    cur.execute("""
        SELECT LOWER(email) FROM contatos_alternativos
        WHERE cnpj=%s OR empresa_dominio=%s
    """, (cnpj, dominio))
    skip = {row[0] for row in cur.fetchall() if row[0]}
    cur.execute("SELECT DISTINCT LOWER(nivel1_email) FROM obras WHERE cnpj=%s AND nivel1_email IS NOT NULL", (cnpj,))
    skip |= {row[0] for row in cur.fetchall() if row[0]}
    print(f"  skip pool: {len(skip)} emails")

    # --- ESTRATÉGIA A: senior/executive, limit=100 ---
    print(f"  [A] domain-search seniority=senior,executive limit=100 ...")
    emails = hunter_domain_search(dominio, limit=100, seniority='senior,executive')
    stats['hunter_calls'] += 1
    print(f"     returned {len(emails)} emails")
    best = pick_best(emails, skip)
    if best:
        apply_pick(cnpj, dominio, label, best, 'domain_senior')
        time.sleep(2); continue

    # --- ESTRATÉGIA B: Serper LinkedIn + Email Finder ---
    print(f"  [B] serper LinkedIn search ...")
    role_q = "diretor OR gerente OR head suprimentos OR compras OR procurement OR engenharia OR projetos OR operações"
    items = serper_linkedin(empresa_simple, role_q)
    stats['serper_calls'] += 1
    print(f"     {len(items)} LinkedIn hits")
    found_b = False
    for item in items[:5]:
        parsed = parse_linkedin_result(item)
        if not parsed:
            continue
        first, last, cargo_snippet = parsed
        tier = cargo_ok(cargo_snippet)
        if not tier:
            continue
        print(f"     try email-finder: {first} {last} (cargo='{cargo_snippet}')")
        r = hunter_email_finder(dominio, first, last)
        stats['hunter_calls'] += 1
        if not r or (r.get('value') or '').lower() in skip:
            print(f"       no email or dup")
            time.sleep(1); continue
        if (r.get('confidence') or 0) < 70:
            print(f"       confidence<70 ({r.get('confidence')})")
            time.sleep(1); continue
        # Replace cargo with the Serper title (more reliable than Hunter's position)
        if not r.get('position'):
            r['position'] = cargo_snippet
        apply_pick(cnpj, dominio, label, r, 'serper_linkedin')
        found_b = True
        time.sleep(2)
        break
    if found_b:
        continue

    # --- ESTRATÉGIA C: type=generic (role-based) ---
    print(f"  [C] domain-search type=generic ...")
    emails = hunter_domain_search(dominio, limit=50, type_='generic')
    stats['hunter_calls'] += 1
    print(f"     {len(emails)} generic emails")
    # Aceita role-based emails que CASEM com P1/P2 mesmo sem position
    role_kw = re.compile(r'(compras|suprimentos|procurement|sourcing|engenharia|projetos?|manuten[cç][aã]o|obras|opera[cç][oõ]es|industrial)', re.I)
    best_c = None
    for em in emails:
        email = (em.get('value') or '').lower()
        if not email or email in skip:
            continue
        local = email.split('@')[0]
        if not role_kw.search(local):
            continue
        score = em.get('confidence') or 0
        if score < 60:  # generic geralmente tem score menor
            continue
        best_c = em
        best_c['position'] = best_c.get('position') or f'role-email:{local}'
        break
    if best_c:
        apply_pick(cnpj, dominio, label, best_c, 'role_generic')
        time.sleep(2); continue

    stats['no_match'] += 1
    print(f"  ↯ NO_MATCH em A+B+C")
    time.sleep(2)

print(f"\n=== STATS ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
cur.close()
conn.close()
