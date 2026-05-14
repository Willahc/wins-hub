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
  WHERE o.classificacao_computed IN ('OURO','PRATA')
    AND o.visivel=true
    AND m.score >= 70
    AND NOT EXISTS (
      SELECT 1 FROM fila_prospeccao fp WHERE fp.fornecedor_cnpj = m.cnpj
    )
)
SELECT fornecedor_cnpj, obra_id, setor, score
FROM ranqueados
WHERE rank_setor <= %s
ORDER BY setor, rank_setor
LIMIT %s;
"""

STOP_RAZAO = re.compile(r'\s+(ltda|s\.?a\.?|me|epp|eireli|s/a|industria|comercio)\b.*', re.I)

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
            }
    except Exception:
        pass
    return {'ativo': None, 'razao': '', 'nome_fantasia': ''}

def inferir_dominio(razao, nome_fantasia=''):
    base = nome_fantasia or razao
    if not base: return None
    s = STOP_RAZAO.sub('', base.lower()).strip()
    s = ''.join(c for c in s if c.isalnum())
    if len(s) < 3 or len(s) > 30: return None
    return f"{s}.com.br"

async def head_check(client, url):
    if not url: return False
    for proto in ('https://','http://'):
        try:
            r = await client.head(f"{proto}{url}", timeout=3, follow_redirects=True)
            if r.status_code < 400:
                return True
        except Exception:
            continue
    return False

async def serper_linkedin(client, razao):
    if not razao: return None
    try:
        r = await client.post(
            'https://google.serper.dev/search',
            headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'},
            json={'q': f'"{razao}" site:linkedin.com/company', 'num': 3, 'gl': 'br', 'hl': 'pt'},
            timeout=10,
        )
        if r.status_code == 200:
            for item in r.json().get('organic', []):
                link = item.get('link','')
                if 'linkedin.com/company' in link:
                    return link.split('?')[0]
    except Exception:
        pass
    return None

async def enriquecer_um(client, c):
    info = await brasil_api(client, c['fornecedor_cnpj'])
    c['cnpj_ativo'] = info['ativo']
    c['razao'] = info['razao']

    if info['ativo'] is False:
        c['site_url'] = None; c['site_ativo'] = False
        c['linkedin_url'] = None
        c['status_digital'] = 'INATIVO'
        return c

    dom = inferir_dominio(info['razao'], info['nome_fantasia'])
    c['site_url'] = dom
    c['site_ativo'] = await head_check(client, dom) if dom else False
    c['linkedin_url'] = await serper_linkedin(client, info['razao'])

    if c['linkedin_url'] and c['site_ativo']:
        c['status_digital'] = 'ATIVO'
    elif c['site_ativo']:
        c['status_digital'] = 'SEM_LINKEDIN'
    elif c['linkedin_url']:
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

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(5)
        async def worker(c):
            async with sem:
                return await enriquecer_um(client, c)
        results = await asyncio.gather(*[worker(c) for c in candidatos])

    inseridas = 0
    for c in results:
        cur.execute("""
            INSERT INTO fila_prospeccao
              (fornecedor_cnpj, obra_id, setor, score_match,
               cnpj_ativo, razao_social, site_url, site_ativo, linkedin_url,
               status_digital, enriquecido_em, rep_atribuido, atribuido_em, lote)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,NOW(),%s)
            ON CONFLICT (fornecedor_cnpj) DO NOTHING
        """, (
            c['fornecedor_cnpj'], c['obra_id'], c['setor'], c['score'],
            c.get('cnpj_ativo'), c.get('razao'), c.get('site_url'),
            c.get('site_ativo'), c.get('linkedin_url'),
            c.get('status_digital'), rep_email, lote_num
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
