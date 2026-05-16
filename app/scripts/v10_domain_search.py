#!/usr/bin/env python3
"""V10 saneamento — Hunter Domain Search nas obras OURO/PRATA com domínio sem decisor.

Diferenças do v9_domain_search.py:
- MAX_DOMAINS=25 (cap conservador)
- Skip gov.br (briefing 16/05: domínios governamentais têm cobertura baixa no Hunter)
- origem='V10_saneamento' nos contatos_alternativos + decisor_status='DISCOVERED_V10'
- ORDER BY max(valor_estimado) DESC (prioriza maior capex)
"""
import os
import re
import time
import json
import urllib.request
import urllib.parse
import psycopg2

API_KEY = os.environ["HUNTER_API_KEY"]
UA = "wins-hub-enrichment/10.0 (+williamvnvn@gmail.com)"
MAX_DOMAINS = 25
SLEEP = 2

EXCLUDE = re.compile(r"presidente|\bceo\b|\bvp\b|vice|marketing|\brh\b|recursos humanos|juridico|jur[ií]dic|financ|comunic", re.I)
P1 = re.compile(r"suprimento|compra|procurement|purchas|sourcing|aquisi", re.I)
P2 = re.compile(r"engenh|projeto|manut|construc|obra|industrial|operac", re.I)
P3 = re.compile(r"gerente|coordena|diretor|head|manager|chefe", re.I)

def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())

def priority(cargo):
    if not cargo:
        return 99
    if EXCLUDE.search(cargo):
        return 100  # rejected
    if P1.search(cargo):
        return 1
    if P2.search(cargo):
        return 2
    if P3.search(cargo):
        return 3
    return 4

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=5432,
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD"),
    dbname=os.environ.get("DB_NAME", "wins_hub"),
)
cur = conn.cursor()
cur.execute("""
    SELECT ed.dominio, ed.cnpj, ed.empresa_nome,
           MAX(o.valor_estimado) AS max_capex,
           COUNT(o.id) AS obras
    FROM obras o
    JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed IN ('OURO','PRATA')
      AND (o.nivel1_nome IS NULL OR o.nivel1_nome = '')
      AND (o.visivel IS NULL OR o.visivel = true)
      AND ed.dominio IS NOT NULL
      AND ed.dominio NOT ILIKE '%%.gov.br'
      AND ed.validacao_metodo NOT ILIKE '%%E2%%'
      AND ed.validacao_metodo NOT ILIKE '%%E3%%'
      AND ed.validacao_metodo NOT ILIKE '%%automatica%%'
    GROUP BY ed.dominio, ed.cnpj, ed.empresa_nome
    ORDER BY MAX(o.valor_estimado) DESC NULLS LAST, COUNT(o.id) DESC
    LIMIT %s
""", (MAX_DOMAINS,))
domains = cur.fetchall()
print(f"[T4] {len(domains)} domains to search", flush=True)

domains_with_decisor = 0
inserts_alt = 0
updates_obras = 0
errors = 0

for idx, (dominio, cnpj, empresa, max_capex, obra_ct) in enumerate(domains, 1):
    url = "https://api.hunter.io/v2/domain-search?" + urllib.parse.urlencode({
        "domain": dominio, "limit": 10, "type": "personal", "api_key": API_KEY
    })
    try:
        data = http_get_json(url)
    except Exception as e:
        errors += 1
        print(f"[{idx}/{len(domains)}] DOMAIN ERR {dominio}: {e}", flush=True)
        time.sleep(SLEEP)
        continue

    emails = data.get("data", {}).get("emails", []) or []
    ranked = []
    for e in emails:
        pos = (e.get("position") or "")
        first = e.get("first_name") or ""
        last = e.get("last_name") or ""
        email = e.get("value")
        score = e.get("confidence") or 0
        status = (e.get("verification") or {}).get("status") or ""
        pri = priority(pos)
        if pri == 100:
            continue
        if status not in ("valid", "accept_all"):
            continue
        if score < 65:
            continue
        if not first or not last or not email:
            continue
        ranked.append((pri, -score, first, last, pos, email, score, status, e.get("linkedin")))

    ranked.sort()
    if not ranked:
        print(f"[{idx}/{len(domains)}] {dominio} ({empresa}): 0 valid candidates", flush=True)
        time.sleep(SLEEP)
        continue

    domains_with_decisor += 1
    best = ranked[0]
    pri, _negscore, fn, ln, pos, email, score, status, linkedin = best
    nome = f"{fn} {ln}".strip()

    cur.execute("""
        UPDATE obras SET
            nivel1_nome = %s,
            nivel1_cargo = %s,
            nivel1_email = %s,
            nivel1_email_smtp_verified = %s,
            nivel1_email_status = %s,
            nivel1_email_score = %s,
            nivel1_email_verified_at = NOW(),
            nivel1_linkedin = %s,
            decisor_status = 'DISCOVERED_V10',
            nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || ' +V10_saneamento'
        WHERE cnpj = %s
          AND classificacao_computed IN ('OURO','PRATA')
          AND (nivel1_nome IS NULL OR nivel1_nome = '')
    """, (nome, pos, email, status in ("valid", "accept_all"), status, score, linkedin, cnpj))
    updates_obras += cur.rowcount

    for pri2, _negs, fn2, ln2, pos2, email2, score2, status2, link2 in ranked[:5]:
        cur.execute("""
            INSERT INTO contatos_alternativos
              (cnpj, empresa_dominio, email, nome, cargo, linkedin_url,
               hunter_score, hunter_status, origem)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'V10_saneamento')
            ON CONFLICT (email) DO NOTHING
        """, (cnpj, dominio, email2, f"{fn2} {ln2}".strip(), pos2, link2, score2, status2))
        inserts_alt += 1

    conn.commit()
    print(f"[{idx}/{len(domains)}] {dominio}: best={email} | {pos} | pri={pri} score={score} status={status}", flush=True)
    time.sleep(SLEEP)

print(f"\n=== T4 RESULT === domains_searched={len(domains)} with_decisor={domains_with_decisor} obras_updated={updates_obras} alt_inserts={inserts_alt} errors={errors}")
cur.close()
conn.close()
