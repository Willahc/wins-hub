"""
Enriquece fornecedores pra fila de prospecção.
Uso: python enriquecer_fila.py --rep mari@... --qtd 100

Estratégia:
- Top N=20 por setor (rotação anti-fadiga, max 5 setores × 20 = 100)
- BrasilAPI: status CNPJ + razão social
- Inferência domínio + HEAD HTTP (3s timeout) pra confirmar site ativo
- Serper LinkedIn /company
- 5 workers paralelos
"""
import asyncio, httpx, os, argparse, re
import psycopg2

SERPER_KEY = os.environ['SERPER_API_KEY']

DB_KW = dict(
    host=os.environ.get('DB_HOST','db'),
    user=os.environ.get('DB_USER','postgres'),
    password=os.environ.get('DB_PASSWORD','WiNS@Hub2026!'),
    dbname=os.environ.get('DB_NAME','wins_hub'),
)

TOP_POR_SETOR = 20

SQL_CANDIDATOS = """
WITH ranqueados AS (
  SELECT
    m.cnpj AS fornecedor_cnpj,
    m.obra_id,
    o.setor,
    m.score,
    ROW_NUMBER() OVER (PARTITION BY o.setor ORDER BY m.score DESC, random()) AS rank_setor
  FROM matches_obra_prestador m
  JOIN obras o ON o.id=m.obra_id
  WHERE o.classificacao_computed IN ('OURO','PRATA','BRONZE')
    AND o.visivel=true
    AND m.score >= 70
    AND NOT EXISTS (
      SELECT 1 FROM fila_prospeccao fp WHERE fp.fornecedor_cnpj = m.cnpj
    )
)
SELECT fornecedor_cnpj, obra_id, setor, score
FROM ranqueados
WHERE rank_setor <= %s
ORDER BY
  CASE setor
    WHEN 'MINERACAO' THEN 1
    WHEN 'ENERGIA' THEN 2
    WHEN 'INFRAESTRUTURA' THEN 3
    WHEN 'INDUSTRIAL' THEN 4
    WHEN 'PORTUARIO' THEN 5
    WHEN 'PETROLEO_GAS' THEN 6
    WHEN 'SUCROENERGETICO' THEN 7
    WHEN 'LATICINIOS' THEN 8
    WHEN 'FRIGORIFICO' THEN 9
    ELSE 99
  END,
  rank_setor
LIMIT %s;
"""

EMAIL_PROVIDERS_GENERICOS = {
    'gmail.com', 'hotmail.com', 'yahoo.com', 'outlook.com', 'live.com',
    'uol.com.br', 'bol.com.br', 'terra.com.br', 'ig.com.br', 'r7.com',
    'icloud.com', 'globo.com', 'globomail.com', 'msn.com', 'aol.com',
    'protonmail.com', 'tutanota.com', 'zoho.com', 'fastmail.com',
}

SKIP_DOMAINS = {
    # Social
    'linkedin.com','facebook.com','instagram.com','youtube.com','twitter.com',
    'x.com','tiktok.com','pinterest.com','wikipedia.org','wikimapia.org',
    # Data brokers / CNPJ lookups
    'cnpj.biz','cnpj.info','cnpja.com','cnpja.com.br','econodata.com.br',
    'consultasocio.com','empresascnpj.com','empresas.serasaexperian.com.br',
    'serasaexperian.com.br','panjiva.com','zoominfo.com','crunchbase.com',
    'dnb.com','escavador.com','cadastronacional.com','casadosdados.com.br',
    'casadosdados.com','triceleads.com','solucoes.cnpj.com','consultacnpj.com',
    'cnpjbrasil.com','cnpj.tributo.com.br','cnpj.tax','consultacnpj.com.br',
    'jusbrasil.com.br','b2brazil.com','empresasdobrasil.com','empresasonline.com.br',
    # Legal / news / blog
    'advdinamico.com.br','tribunais.jus.br','jus.br','bnamericas.com',
    'infomoney.com.br','valor.com.br','folha.com.br','globo.com',
    'gov.br','receita.fazenda.gov.br','b3.com.br','anbima.com.br',
    # Job sites
    'glassdoor.com','glassdoor.com.br','vagas.com.br','indeed.com',
    'catho.com.br','infojobs.com.br','empregos.com.br','gupy.io',
    # Search engines
    'google.com','maps.google.com','bing.com','duckduckgo.com',
    # Marketplaces
    'mercadolivre.com.br','olx.com.br','amazon.com','amazon.com.br','ebay.com',
}

import unicodedata

def _deaccent(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def _tokens_razao(razao):
    """Tokens >=4 chars do razão social, sem acento, sem stopwords."""
    stop = {'ltda','sa','industria','comercio','engenharia','consultoria','servicos',
            'representacoes','solucoes','sustainable','solution','transportes','holding',
            'participacoes','do','de','da','dos','das','e','&','-','limitada','epp','me',
            'eireli','grupo','brasil','brazil','associacao','cia','companhia','industrial'}
    text = _deaccent(razao or '').lower()
    text = re.sub(r'[^a-z0-9 ]', ' ', text)
    return [t for t in text.split() if len(t) >= 4 and t not in stop]

def _dominio_do_email(email):
    """Extrai dominio corporativo do email (skip providers genéricos)."""
    if not email or '@' not in email:
        return None
    dom = email.split('@', 1)[1].strip().lower()
    if not dom or dom in EMAIL_PROVIDERS_GENERICOS:
        return None
    # Falsos positivos: contadores, advocacias, consultorias fiscais
    padroes_fp = ('contabil', 'contabilidade', 'advocac', 'fiscal', 'tributar',
                  'assessoria', 'escritorioc', 'shopping', 'savian', 'fortnort')
    if any(p in dom for p in padroes_fp):
        return None
    return dom

def _site_matches_razao(host, razao):
    """Host plausivelmente da empresa: contém ao menos 1 token >=4 chars do razão."""
    if not host or not razao: return False
    host_clean = re.sub(r'[^a-z0-9]', '', host.lower())
    for tok in _tokens_razao(razao):
        if tok in host_clean:
            return True
    return False

async def brasil_api(client, cnpj):
    cnpj_limpo = ''.join(c for c in cnpj if c.isdigit())
    try:
        r = await client.get(f'https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}', timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {
                'ativo': (d.get('descricao_situacao_cadastral') or '').upper() == 'ATIVA',
                'razao': (d.get('razao_social') or '').strip(),
                'nome_fantasia': (d.get('nome_fantasia') or '').strip(),
                'email_rfb': (d.get('email') or '').strip().lower() or None,
                'telefone_rfb': (d.get('ddd_telefone_1') or '').strip() or None,
            }
    except Exception:
        pass
    return {'ativo': None, 'razao': '', 'nome_fantasia': '', 'email_rfb': None, 'telefone_rfb': None}

async def get_check(client, dom):
    """GET é mais permissivo que HEAD (alguns servidores rejeitam HEAD)."""
    if not dom: return False
    for proto in ('https://','http://'):
        try:
            r = await client.get(f"{proto}{dom}", timeout=5, follow_redirects=True)
            if r.status_code < 400:
                return True
        except Exception:
            continue
    return False

async def serper_descobrir(client, razao, cnpj):
    """1 search genérico → extrai site oficial + LinkedIn da MESMA resposta.
    Filtra data-brokers via SKIP_DOMAINS + EXIGE host conter token >=4 chars
    do razão social (rejeita data brokers tipo solutudo, empresaqui que escapam
    do skip-list por serem "directories" novos).
    """
    if not razao: return None, None
    linkedin = None
    site = None
    try:
        r = await client.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'},
            json={'q': f'"{razao}" CNPJ', 'num': 10, 'gl': 'br', 'hl': 'pt'},
            timeout=12,
        )
        if r.status_code != 200:
            return None, None
        for item in r.json().get('organic', []):
            link = (item.get('link') or '')
            if not link:
                continue
            if 'linkedin.com/company' in link and not linkedin:
                linkedin = link.split('?')[0]
                continue
            if site is not None:
                continue
            try:
                host = link.split('?')[0].split('/')[2].lower().replace('www.','')
            except IndexError:
                continue
            base = '.'.join(host.split('.')[-2:])
            if host in SKIP_DOMAINS or base in SKIP_DOMAINS:
                continue
            if any(s in host for s in SKIP_DOMAINS):
                continue
            # STRICT: precisa casar com token >=4 chars do razao
            # (rejeita directories tipo solutudo.com.br, empresaqui.com.br)
            if not _site_matches_razao(host, razao):
                continue
            site = host
    except Exception:
        pass
    return site, linkedin


def _fornecedor_extras(conn, cnpj):
    """Pega email/telefone diretos da tabela fornecedores (RFB import)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT email, telefone_1
            FROM fornecedores WHERE cnpj=%s LIMIT 1
        """, (cnpj,))
        row = cur.fetchone()
        if row:
            return {'email': (row[0] or '').strip() or None, 'telefone': (row[1] or '').strip() or None}
    return {'email': None, 'telefone': None}

async def enriquecer_um(client, c, extras):
    info = await brasil_api(client, c['fornecedor_cnpj'])
    c['cnpj_ativo'] = info['ativo']
    c['razao'] = info['razao']
    # Prioridade email: BrasilAPI (mais fresh) > fornecedores import RFB
    c['email_generico'] = info.get('email_rfb') or extras.get('email')

    if info['ativo'] is False:
        c['site_url'] = None
        c['site_ativo'] = False
        c['linkedin_url'] = None
        c['status_digital'] = 'INATIVO'
        return c

    # FALLBACK 1: domain do email RFB (free, 1 GET) — mais confiável que Serper match
    email_dom = _dominio_do_email(c['email_generico'])
    site_via_email = email_dom if (email_dom and await get_check(client, email_dom)) else None

    # Serper sempre pra ter LinkedIn (e site alternativo se email falhou)
    site_serper, linkedin = await serper_descobrir(client, info['razao'], c['fornecedor_cnpj'])
    c['linkedin_url'] = linkedin

    # Prioriza site validado via email (token match strict falha em empresas-mãe/marcas)
    if site_via_email:
        c['site_url'] = site_via_email
        c['site_ativo'] = True
    else:
        c['site_url'] = site_serper
        c['site_ativo'] = await get_check(client, site_serper) if site_serper else False

    if c['site_ativo'] and linkedin:
        c['status_digital'] = 'ATIVO'
    elif c['site_ativo']:
        c['status_digital'] = 'SEM_LINKEDIN'
    elif linkedin:
        c['status_digital'] = 'SEM_SITE'
    else:
        c['status_digital'] = 'INVALIDO'
    return c

async def main(rep_email, qtd):
    conn = psycopg2.connect(**DB_KW)
    cur = conn.cursor()

    cur.execute(SQL_CANDIDATOS, (TOP_POR_SETOR, qtd))
    rows = cur.fetchall()
    if not rows:
        print('Sem candidatos novos.'); cur.close(); conn.close(); return

    candidatos = [{'fornecedor_cnpj': r[0], 'obra_id': r[1], 'setor': r[2], 'score': float(r[3] or 0)} for r in rows]
    print(f'Enriquecendo {len(candidatos)} candidatos pra {rep_email}...', flush=True)

    cur.execute("SELECT COALESCE(MAX(lote),0)+1 FROM fila_prospeccao WHERE rep_atribuido=%s", (rep_email,))
    lote_num = cur.fetchone()[0]

    # Pré-busca emails/telefones existentes em fornecedores
    extras_por_cnpj = {c['fornecedor_cnpj']: _fornecedor_extras(conn, c['fornecedor_cnpj']) for c in candidatos}

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(5)
        async def worker(c):
            async with sem:
                return await enriquecer_um(client, c, extras_por_cnpj.get(c['fornecedor_cnpj'], {}))
        results = await asyncio.gather(*[worker(c) for c in candidatos])

    inseridas = 0
    for c in results:
        cur.execute("""
            INSERT INTO fila_prospeccao
              (fornecedor_cnpj, obra_id, setor, score_match,
               cnpj_ativo, razao_social, site_url, site_ativo, linkedin_url,
               email_generico, status_digital, enriquecido_em,
               rep_atribuido, atribuido_em, lote)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,NOW(),%s)
            ON CONFLICT (fornecedor_cnpj) DO NOTHING
        """, (
            c['fornecedor_cnpj'], c['obra_id'], c['setor'], c['score'],
            c.get('cnpj_ativo'), c.get('razao'), c.get('site_url'),
            c.get('site_ativo'), c.get('linkedin_url'),
            c.get('email_generico'), c.get('status_digital'),
            rep_email, lote_num
        ))
        if cur.rowcount: inseridas += 1
    conn.commit()

    cur.execute("""
      SELECT status_digital, COUNT(*) FROM fila_prospeccao
      WHERE lote=%s AND rep_atribuido=%s GROUP BY 1 ORDER BY 2 DESC
    """, (lote_num, rep_email))
    print(f'\nLote {lote_num} criado ({inseridas} inserções):', flush=True)
    for sd, ct in cur.fetchall():
        print(f'  {sd}: {ct}', flush=True)

    cur.close(); conn.close()

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--rep', required=True)
    ap.add_argument('--qtd', type=int, default=100)
    args = ap.parse_args()
    asyncio.run(main(args.rep, args.qtd))
