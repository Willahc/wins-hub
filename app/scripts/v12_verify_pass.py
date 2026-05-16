#!/usr/bin/env python3
"""V12 verify pass — domain-search + email-verifier explícito.

Pra obras OURO/PRATA com domínio onde V10/V11 retornaram 0 valid candidates
(Hunter devolveu emails mas com verification status vazio).

Estratégia:
  1. domain-search limit=10
  2. ranqueia por (priority cargo, score)
  3. pega top 5
  4. pra cada um: chama /v2/email-verifier (custa 1 verification, não search)
  5. aceita status in (valid, accept_all, webmail)
  6. UPDATE obras nivel1_* com o melhor confirmado
"""
import os
import re
import time
import json
import urllib.request
import psycopg2

API_KEY = os.environ["HUNTER_API_KEY"]
UA = "wins-hub-enrichment/12.0 (+williamvnvn@gmail.com)"
SLEEP = 1.5
MAX_DOMAINS = 100
MAX_VERIFY_PER_DOMAIN = 5

EXCLUDE = re.compile(r"presidente|\bceo\b|\bvp\b|vice|marketing|\brh\b|recursos humanos|juridico|jur[ií]dic|financ|comunic|legal", re.I)
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
        return 100
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
           MAX(o.valor_estimado) AS max_capex
    FROM obras o
    JOIN empresa_dominios ed ON o.cnpj = ed.cnpj
    WHERE o.classificacao_computed IN ('OURO','PRATA')
      AND (o.nivel1_nome IS NULL OR o.nivel1_nome = '')
      AND (o.visivel IS NULL OR o.visivel = true)
      AND ed.dominio IS NOT NULL
    GROUP BY ed.dominio, ed.cnpj, ed.empresa_nome
    ORDER BY MAX(o.valor_estimado) DESC NULLS LAST
    LIMIT %s
""", (MAX_DOMAINS,))
domains = cur.fetchall()
print(f"[V12] {len(domains)} domains to verify", flush=True)

with_decisor = obras_updated = inserts_alt = errors = 0
verifications_used = 0

for idx, (dominio, cnpj, empresa, max_capex) in enumerate(domains, 1):
    try:
        ds = http_get_json(
            f"https://api.hunter.io/v2/domain-search?domain={dominio}&limit=10&api_key={API_KEY}"
        )
    except Exception as e:
        errors += 1
        print(f"[{idx}/{len(domains)}] {dominio} DS ERR {e}", flush=True)
        time.sleep(SLEEP)
        continue

    emails = ds.get("data", {}).get("emails", []) or []
    ranked = []
    for e in emails:
        pos = e.get("position") or ""
        first = e.get("first_name") or ""
        last = e.get("last_name") or ""
        email = e.get("value")
        score = e.get("confidence") or 0
        pri = priority(pos)
        if pri == 100:
            continue
        if not email or not first or not last:
            continue
        if score < 60:
            continue
        ranked.append((pri, -score, first, last, pos, email, score, e.get("linkedin")))

    ranked.sort()
    if not ranked:
        print(f"[{idx}/{len(domains)}] {dominio} ({empresa}): 0 candidates after filter (pre-verify)", flush=True)
        time.sleep(SLEEP)
        continue

    chosen = None
    for cand in ranked[:MAX_VERIFY_PER_DOMAIN]:
        pri, _ns, fn, ln, pos, email, score, lnk = cand
        try:
            vr = http_get_json(
                f"https://api.hunter.io/v2/email-verifier?email={email}&api_key={API_KEY}"
            )
            verifications_used += 1
        except Exception as e:
            print(f"  verify ERR {email}: {e}", flush=True)
            time.sleep(SLEEP)
            continue
        vd = vr.get("data", {}) or {}
        vstatus = vd.get("status") or "?"
        vscore = vd.get("score") or 0
        if vstatus in ("valid", "accept_all", "webmail"):
            chosen = (pri, -vscore, fn, ln, pos, email, vscore, vstatus, lnk)
            break
        time.sleep(SLEEP)

    if not chosen:
        print(f"[{idx}/{len(domains)}] {dominio} ({empresa}): {len(ranked)} candidates, NONE verified valid", flush=True)
        time.sleep(SLEEP)
        continue

    with_decisor += 1
    pri, _ns, fn, ln, pos, email, vscore, vstatus, lnk = chosen
    nome = f"{fn} {ln}".strip()
    cur.execute("""
        UPDATE obras SET
            nivel1_nome = %s,
            nivel1_cargo = %s,
            nivel1_email = %s,
            nivel1_email_smtp_verified = TRUE,
            nivel1_email_status = %s,
            nivel1_email_score = %s,
            nivel1_email_verified_at = NOW(),
            nivel1_linkedin = %s,
            decisor_status = 'DISCOVERED_V12',
            nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment,'') || ' +V12_verify'
        WHERE cnpj = %s
          AND classificacao_computed IN ('OURO','PRATA')
          AND (nivel1_nome IS NULL OR nivel1_nome = '')
    """, (nome, pos, email, vstatus, vscore, lnk, cnpj))
    obras_updated += cur.rowcount

    # Insere até 5 alternativos do ranked
    for cand in ranked[:5]:
        pri2, _ns, fn2, ln2, pos2, email2, score2, lnk2 = cand
        cur.execute("""
            INSERT INTO contatos_alternativos
              (cnpj, empresa_dominio, email, nome, cargo, linkedin_url,
               hunter_score, hunter_status, origem)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'V12_verify')
            ON CONFLICT (email) DO NOTHING
        """, (cnpj, dominio, email2, f"{fn2} {ln2}".strip(), pos2, lnk2, score2, "candidate"))
        inserts_alt += 1
    conn.commit()
    print(f"[{idx}/{len(domains)}] {dominio}: best={email} | {pos} | pri={pri} score={vscore} status={vstatus}", flush=True)
    time.sleep(SLEEP)

print(f"\n=== V12 RESULT === domains={len(domains)} with_decisor={with_decisor} obras_updated={obras_updated} alt_inserts={inserts_alt} verifications_used={verifications_used} errors={errors}")
cur.close()
conn.close()
