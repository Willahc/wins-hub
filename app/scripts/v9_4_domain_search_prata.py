"""V9.4 — Hunter Domain Search nas 18 PRATA sem origem.
Insere em contatos_alternativos + UPDATE obras.nivel1_* quando cargo bate P1/P2.
"""
import os, time, requests, psycopg2, re

HUNTER_KEY = os.environ['HUNTER_API_KEY']

P1 = re.compile(r'\b(suprimentos?|compras|procurement|engenharia|projetos?|engineer|engineering|sourcing|supply\s*chain)\b', re.I)
P2 = re.compile(r'\b(ger[eê]ncia|gerente|coordena[cç][aã]o|coordenador|head\b|director\b|diretor\b|manager\b|supervisor)\b', re.I)
EXCLUDE = re.compile(
    r'\b(presidente|ceo\b|vp\b|vice[\s-]presidente|c[eo]o\b|cfo\b|cto\b|coo\b|cmo\b|marketing|'
    r'rh\b|recursos\s*humanos|human\s*resources|jur[ií]dico|legal\b|advogad[oa]|'
    r'financ|finance|comunic|communicat|press|imprensa|midia|m[ií]dia|'
    r'investor|relat[oó]rio|relations|sustentabil|esg\b|ti\b|tecnologia\s*da)\b', re.I)

def is_valid_cargo(cargo):
    if not cargo:
        return False
    if EXCLUDE.search(cargo):
        return False
    return bool(P1.search(cargo) or P2.search(cargo))

def hunter_domain_search(domain):
    url = "https://api.hunter.io/v2/domain-search"
    params = {
        'domain': domain,
        'api_key': HUNTER_KEY,
        'limit': 10,
        'type': 'personal',
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        return r.json().get('data', {}).get('emails', [])
    except Exception as e:
        print(f"  ERR hunter {domain}: {e}")
        return []

def pick_best(emails):
    """Filtra por cargo válido + score>=65, P1 preferido sobre P2."""
    candidates = []
    for em in emails:
        cargo = em.get('position') or em.get('department') or ''
        score = em.get('confidence') or 0
        email = em.get('value') or ''
        if not email or score < 65:
            continue
        if not is_valid_cargo(cargo):
            continue
        tier = 1 if P1.search(cargo) else 2
        candidates.append((tier, -score, email, em))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][3]

conn = psycopg2.connect(
    host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'])
cur = conn.cursor()

cur.execute("""
    SELECT DISTINCT ed.dominio, ed.cnpj, MAX(o.empresa) AS empresa
    FROM obras o
    JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed = 'PRATA'
      AND (o.nivel1_origem_enrichment IS NULL OR o.nivel1_origem_enrichment = '')
      AND ed.dominio IS NOT NULL
    GROUP BY ed.dominio, ed.cnpj
    ORDER BY ed.dominio
""")
targets = cur.fetchall()
print(f"Targets: {len(targets)}")

CAP = 18
stats = {'searched': 0, 'found': 0, 'no_match': 0, 'inserted': 0, 'updated_obras': 0}

for dominio, cnpj, empresa in targets[:CAP]:
    print(f"\n--- {empresa} ({cnpj}) @ {dominio} ---")
    stats['searched'] += 1
    emails = hunter_domain_search(dominio)
    print(f"  hunter returned {len(emails)} emails")
    best = pick_best(emails)
    if not best:
        stats['no_match'] += 1
        print(f"  NO_MATCH (sem cargo P1/P2 com score>=65)")
        time.sleep(2)
        continue

    nome = ((best.get('first_name') or '') + ' ' + (best.get('last_name') or '')).strip()
    cargo = best.get('position') or best.get('department') or ''
    email = best.get('value')
    score = best.get('confidence') or 0
    linkedin = best.get('linkedin') or None
    verif_status = (best.get('verification') or {}).get('status') or 'unknown'
    smtp_ok = (verif_status == 'valid')
    departamento = best.get('department') or ''

    print(f"  PICK: {nome} | {cargo} | {email} | score={score} | smtp={verif_status}")
    stats['found'] += 1

    cur.execute("""
        INSERT INTO contatos_alternativos
          (cnpj, empresa_dominio, email, nome, cargo, departamento, linkedin_url,
           hunter_score, hunter_status, origem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'V9_prata_domain_search')
        ON CONFLICT (email) DO NOTHING
        RETURNING id
    """, (cnpj, dominio, email, nome, cargo, departamento, linkedin, score, verif_status))
    inserted = cur.fetchone()
    if inserted:
        stats['inserted'] += 1

    cur.execute("""
        UPDATE obras
        SET nivel1_nome = %s,
            nivel1_cargo = %s,
            nivel1_email = %s,
            nivel1_email_smtp_verified = %s,
            nivel1_email_score = %s,
            nivel1_email_status = %s,
            decisor_status = 'DISCOVERED_V9_PRATA',
            nivel1_origem_enrichment = 'V9_prata_domain_search'
        WHERE cnpj = %s
          AND classificacao_computed = 'PRATA'
          AND (nivel1_origem_enrichment IS NULL OR nivel1_origem_enrichment = '')
    """, (nome, cargo, email, smtp_ok, score, verif_status, cnpj))
    n_upd = cur.rowcount
    stats['updated_obras'] += n_upd
    print(f"  obras updated: {n_upd}")
    conn.commit()
    time.sleep(2)

print(f"\n=== STATS ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
cur.close()
conn.close()
