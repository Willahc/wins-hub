import os, time, requests, psycopg2, re
from urllib.parse import urlparse

SERPER_KEY = os.environ['SERPER_API_KEY']

SKIP_DOMAINS = {
    # Social/Media
    'linkedin.com','facebook.com','instagram.com','youtube.com','twitter.com','x.com',
    'wikipedia.org','wikimapia.org','tiktok.com','pinterest.com',
    # Data brokers / company lookup
    'reclameaqui.com.br','cnpj.biz','econodata.com.br','cnpja.com','cnpj.info',
    'consultasocio.com','empresascnpj.com','empresas.serasaexperian.com.br',
    'serasaexperian.com.br','escavador.com','cadastronacional.com','panjiva.com',
    'zoominfo.com','crunchbase.com','dnb.com','dun-bradstreet.com',
    'b2brazil.com','buybrazil.com','emis.com','importgenius.com',
    'thetradevision.com','data.anbima.com.br','anbima.com.br',
    # News
    'bloomberg.com','infomoney.com.br','valor.com.br','noticias.r7.com','r7.com',
    'noticiasagricolas.com.br','bnamericas.com','publicidadelegal.gazetasp.com.br',
    'gazetasp.com.br','dairyindustries.com',
    # Government / legal / regulatory
    'gov.br','receita.fazenda.gov.br','jusbrasil.com.br','tjro.jus.br',
    'tj.jus.br','jus.br','marinha.mil.br','ipenbrasil.org.br','ccee.org.br','b3.com.br',
    'advdinamico.com.br',
    # Job sites
    'glassdoor.com.br','vagas.com.br','indeed.com','catho.com.br',
    'infojobs.com.br','empregos.com.br',
    # Marketplaces
    'ebay.com','mercadolivre.com.br','amazon.com','amazon.com.br',
    # Associations
    'ibram.org.br','abiec.com.br','abiec.org.br','brsa.org.br','agencia.org.br',
    # Random/unrelated
    'maps.google.com','goo.gl','bit.ly','google.com',
    'stonebuyandsell.com','pebinhadeacucar.com.br','riomarketdistribuidora.com.br',
    'calcariobotuvera.com.br','vibrita.com.br','reallacto.com.br',
}

GENERIC_TOKENS = {'ltda','sa','s/a','s.a','industria','comercio','industrial','indústria',
                  'empreendimentos','do','de','da','e','&','-','—','recuperacao','judicial',
                  'em','manutencao','manutenção','opex','ampliacao','ampliação','fabril',
                  'nova','fabrica','fábrica','retrofit','termico','térmico','/','ind','es'}

def clean_name(name):
    n = re.sub(r'[^a-zA-Z0-9\s]', ' ', name.lower())
    return ' '.join(t for t in n.split() if t not in GENERIC_TOKENS and len(t) > 2)

def domain_matches_name(domain, name_clean):
    """Heurística: ao menos um token >=4 chars do nome aparecer no domínio."""
    if not name_clean:
        return False
    tokens = [t for t in name_clean.split() if len(t) >= 4]
    domain_clean = re.sub(r'[^a-z0-9]', '', domain.lower())
    return any(t in domain_clean for t in tokens[:3])

def search_domain(empresa_nome):
    name_clean = clean_name(empresa_nome)
    try:
        r = requests.post('https://google.serper.dev/search',
            headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'},
            json={'q': f'"{empresa_nome}" site oficial', 'hl': 'pt', 'gl': 'br', 'num': 8},
            timeout=15)
        results = r.json().get('organic', [])
    except Exception as e:
        print(f"  ERR serper: {e}")
        return None
    for res in results:
        url = res.get('link', '')
        if not url:
            continue
        domain = urlparse(url).netloc.replace('www.','').lower()
        if not domain:
            continue
        # Skip-list check
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        # Heurística de match com o nome
        if not domain_matches_name(domain, name_clean):
            continue
        # Strip subdomínios "ri." (relações investidores) e similares
        if domain.startswith('ri.') or domain.startswith('investidores.'):
            parts = domain.split('.', 1)
            domain = parts[1] if len(parts) > 1 else domain
        return domain
    return None

conn = psycopg2.connect(
    host=os.environ['DB_HOST'],
    user=os.environ['DB_USER'],
    password=os.environ['DB_PASSWORD'],
    dbname=os.environ['DB_NAME'],
)
cur = conn.cursor()

cur.execute("""
    SELECT DISTINCT o.empresa, o.cnpj
    FROM obras o
    LEFT JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed IN ('OURO','PRATA')
      AND ed.dominio IS NULL
      AND o.cnpj IS NOT NULL
      AND o.empresa IS NOT NULL
      AND o.empresa <> ''
    ORDER BY o.empresa
""")
rows = cur.fetchall()
print(f"Empresas ainda sem domínio (pass 2): {len(rows)}")

found = 0
not_found = []
for empresa, cnpj in rows:
    dominio = search_domain(empresa)
    if dominio:
        cur.execute("""
            INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, fonte, confianca, validacao_metodo, validacao_data)
            VALUES (%s, %s, %s, 'serper_auto', 3, 'chromium_v9_auto', NOW())
            ON CONFLICT (cnpj) DO UPDATE
              SET dominio=EXCLUDED.dominio, fonte=EXCLUDED.fonte,
                  confianca=EXCLUDED.confianca, validacao_metodo=EXCLUDED.validacao_metodo,
                  validacao_data=NOW()
              WHERE empresa_dominios.validacao_metodo NOT IN
                ('manual_chat_v6','manual_chat_v7_correcao','manual_chat_v9_dominios')
        """, (cnpj, empresa, dominio))
        conn.commit()
        found += 1
        print(f"FOUND: {empresa[:60]} -> {dominio}")
    else:
        not_found.append(empresa)
        print(f"NOT_FOUND: {empresa[:60]}")
    time.sleep(1.5)

print(f"\n=== Pass 2 encontrados: {found}/{len(rows)} ===")
print(f"NOT_FOUND ({len(not_found)}):")
for e in not_found:
    print(f"  - {e}")
cur.close()
conn.close()
