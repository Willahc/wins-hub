"""pattern_verify.py — para candidatos Serper LinkedIn que Hunter /email-finder retornou None,
tenta padrões comuns + Hunter /email-verifier; insere em decisores_obra se valid+score>=70.

Padrões testados (por ordem):
  1. {first}.{last}@dom
  2. {f}{last}@dom    (inicial + sobrenome)
  3. {first}@dom      (só primeiro nome)

Hunter custo: até 3 calls/pessoa. Total alvo: 8 pessoas × 3 = 24 calls (saldo atual ~500).
"""
import os, sys, json, time, logging, unicodedata, re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import psycopg2

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', stream=sys.stdout)
log = logging.getLogger('pattern_verify')

HUNTER_KEY = os.environ['HUNTER_API_KEY']
UA = "WiNS-Hub/pattern_verify"

# (cnpj, [obra_id,...], nome_completo, cargo_inferido_da_serper)
TARGETS = [
    ('12091809000155', ['9e393693-a4a0-4590-9fe5-1bbb0890b7fd','8acfd38b-b911-4241-a196-14f15b4d9f84'],
     'Luiz Augusto Pinheiro', 'Executivo Brava Energia'),
    ('12091809000155', ['9e393693-a4a0-4590-9fe5-1bbb0890b7fd','8acfd38b-b911-4241-a196-14f15b4d9f84'],
     'Sergio Tuffy Sayeg', 'Executivo Brava Energia'),
    ('09478309000166', ['73bd2b50-1fe9-4cba-8008-6b067c822de2','f1a47408-095d-4bf0-a561-2d0f62211051',
                         '55a2eefe-3377-4370-ab50-91d29c6bf902','5a33914f-4190-48a5-b844-68892c92ecaf'],
     'Raphael Elmescany', 'Executivo Brasil Bio Fuels'),
    ('09478309000166', ['73bd2b50-1fe9-4cba-8008-6b067c822de2','f1a47408-095d-4bf0-a561-2d0f62211051',
                         '55a2eefe-3377-4370-ab50-91d29c6bf902','5a33914f-4190-48a5-b844-68892c92ecaf'],
     'Roberto Tarimatari', 'Executivo Brasil Bio Fuels'),
    ('09478309000166', ['73bd2b50-1fe9-4cba-8008-6b067c822de2','f1a47408-095d-4bf0-a561-2d0f62211051',
                         '55a2eefe-3377-4370-ab50-91d29c6bf902','5a33914f-4190-48a5-b844-68892c92ecaf'],
     'Michelle Gomez Blagitz', 'Executivo Brasil Bio Fuels'),
    ('09478309000166', ['73bd2b50-1fe9-4cba-8008-6b067c822de2','f1a47408-095d-4bf0-a561-2d0f62211051',
                         '55a2eefe-3377-4370-ab50-91d29c6bf902'],
     'Nicola Di Matteo', 'Executivo Brasil Bio Fuels'),
    ('09478309000166', ['5a33914f-4190-48a5-b844-68892c92ecaf'],
     'Luis Sebastian Villar', 'Executivo Brasil Bio Fuels'),
    ('42500384000151', ['7b58bfb3-113f-4a56-b105-cb6821ab249f'],
     'Jonathan Saul Zumerkorn', 'Executivo Serena Energia'),
]

CNPJ_TO_DOMAIN = {}


def normalize(s: str) -> str:
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii').lower()
    return re.sub(r'[^a-z]', '', s)


def patterns_for(nome: str, dominio: str):
    parts = [p for p in nome.strip().split() if p]
    if len(parts) < 2:
        return []
    first = normalize(parts[0])
    last  = normalize(parts[-1])
    if not first or not last:
        return []
    return [
        f"{first}.{last}@{dominio}",
        f"{first[0]}{last}@{dominio}",
        f"{first}@{dominio}",
    ]


def verify_email(email: str):
    qs = urlencode({'email': email, 'api_key': HUNTER_KEY})
    try:
        req = Request(f'https://api.hunter.io/v2/email-verifier?{qs}', headers={'User-Agent': UA})
        r = json.loads(urlopen(req, timeout=20).read())
        d = r.get('data') or {}
        return d.get('status'), d.get('score') or 0, d.get('regexp'), d.get('disposable'), d.get('webmail')
    except Exception as e:
        log.warning(f"verifier falhou para {email}: {e}")
        return None, 0, None, None, None


def main():
    commit = '--commit' in sys.argv
    conn = psycopg2.connect(host=os.environ['DB_HOST'], user=os.environ['DB_USER'],
                            password=os.environ['DB_PASSWORD'], dbname=os.environ['DB_NAME'])
    cur = conn.cursor()
    # Resolve dominios cacheados
    cnpjs = list({t[0] for t in TARGETS})
    cur.execute("SELECT cnpj, dominio FROM empresa_dominios WHERE cnpj = ANY(%s) AND dominio IS NOT NULL", (cnpjs,))
    for cnpj, dom in cur.fetchall():
        CNPJ_TO_DOMAIN[cnpj] = dom
    log.info(f"dominios cacheados: {CNPJ_TO_DOMAIN}")

    stats = {'tested': 0, 'verifier_calls': 0, 'valid': 0, 'accept_all': 0, 'invalid': 0,
             'inserted': 0, 'skipped_existing': 0}

    for cnpj, obra_ids, nome, cargo in TARGETS:
        dom = CNPJ_TO_DOMAIN.get(cnpj)
        if not dom:
            log.warning(f"sem dominio cacheado cnpj={cnpj} pessoa={nome} — skip")
            continue
        log.info(f"\n▶ {nome} @ {dom} (cnpj={cnpj}, {len(obra_ids)} obras)")
        stats['tested'] += 1
        pats = patterns_for(nome, dom)
        if not pats:
            log.warning(f"  sem padrões gerados — skip")
            continue
        found_email = None
        found_score = 0
        found_status = None
        for email in pats:
            stats['verifier_calls'] += 1
            status, score, regex_ok, disposable, webmail = verify_email(email)
            log.info(f"  {email} -> status={status} score={score}")
            time.sleep(0.7)
            if status == 'valid' and score >= 70 and not disposable:
                found_email, found_score, found_status = email, score, status
                stats['valid'] += 1
                break
            elif status == 'accept_all' and score >= 70:
                # catch-all aceita tudo, marca mas continua tentando mais específico
                stats['accept_all'] += 1
                if not found_email:  # registra primeiro accept_all como fallback
                    found_email, found_score, found_status = email, score, status
            else:
                stats['invalid'] += 1
        if not found_email:
            log.info(f"  ✗ nenhum padrão validou")
            continue
        log.info(f"  ✓ ACHADO {found_email} status={found_status} score={found_score}")
        if not commit:
            continue
        for obra_id in obra_ids:
            try:
                cur.execute("""
                    INSERT INTO decisores_obra
                        (obra_id, nome, cargo, email, fonte, registrado_por, observacoes,
                         confianca_match, confianca_match_componentes, confianca_match_calculada_em)
                    VALUES (%s, %s, %s, %s, 'hunter_pattern_verifier', 'pattern_verify:v1:20260524',
                            %s, %s, %s, NOW())
                    ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
                    RETURNING id
                """, (obra_id, nome, cargo, found_email,
                      f"pattern-guess + Hunter verifier status={found_status} score={found_score}",
                      found_score,
                      json.dumps({'pattern_verifier': True, 'hunter_status': found_status, 'hunter_score': found_score})))
                row = cur.fetchone()
                if row:
                    stats['inserted'] += 1
                    log.info(f"    INSERT obra={obra_id}")
                else:
                    stats['skipped_existing'] += 1
                    log.info(f"    SKIP obra={obra_id} (já existe)")
            except Exception as e:
                conn.rollback()
                log.warning(f"    INSERT falhou obra={obra_id}: {e}")
                continue
        conn.commit()

    log.info(f"\n══ STATS ══ {stats}")
    cur.close(); conn.close()

if __name__ == '__main__':
    main()
