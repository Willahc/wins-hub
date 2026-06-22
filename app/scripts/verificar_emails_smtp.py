#!/usr/bin/env python3
"""
verificar_emails_smtp.py — Verificação de email GRÁTIS (MX + SMTP RCPT), sem Hunter.
Agrupa por domínio (1 conexão/domínio), detecta catch-all, cacheia MX e catch-all.

Status gravados em decisores_obra.email_smtp_status:
  valid | invalid | accept_all | no_mx | syntax | unknown
+ email_smtp_em.

Uso:
  # calibrar contra o gabarito Hunter (decisores já com email_status):
  docker exec wins_hub-api-1 python /app/scripts/verificar_emails_smtp.py --calibrate --commit
  # rodar nos não-verificados:
  docker exec wins_hub-api-1 python /app/scripts/verificar_emails_smtp.py --commit --limit 4000
"""
import argparse, os, re, smtplib, socket, sys, time, random, hashlib
from collections import defaultdict
import dns.resolver
import psycopg2, psycopg2.extras

DB = {"host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
      "dbname": os.getenv("DB_NAME","wins_hub"), "user": os.getenv("DB_USER","postgres"),
      "password": os.getenv("DB_PASSWORD","")}
HELO = "mail.winshub.com.br"
MAILFROM = "verificacao@winshub.com.br"
RE_SYNTAX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
socket.setdefaulttimeout(12)

_mx_cache, _catchall_cache = {}, {}

def get_mx(dom):
    if dom in _mx_cache: return _mx_cache[dom]
    hosts = []
    try:
        ans = dns.resolver.resolve(dom, "MX", lifetime=8)
        hosts = [str(r.exchange).rstrip(".") for r in sorted(ans, key=lambda r: r.preference)]
    except Exception:
        try:  # fallback: A record aceita mail às vezes
            dns.resolver.resolve(dom, "A", lifetime=6); hosts = [dom]
        except Exception:
            hosts = []
    _mx_cache[dom] = hosts
    return hosts

def probe_domain(dom, emails):
    out = {}
    mx = get_mx(dom)
    if not mx:
        return {e: "no_mx" for e in emails}
    try:
        srv = smtplib.SMTP(timeout=12); srv.connect(mx[0], 25)
        srv.helo(HELO); srv.mail(MAILFROM)
        # catch-all (cache por domínio)
        if dom not in _catchall_cache:
            rnd = "nx-%d-probe" % random.randint(10000, 99999)
            try:
                code, _ = srv.rcpt(f"{rnd}@{dom}")
            except Exception:
                code = 0
            _catchall_cache[dom] = code in (250, 251)
        catchall = _catchall_cache[dom]
        for e in emails:
            if catchall:
                out[e] = "accept_all"; continue
            try:
                code, _ = srv.rcpt(e)
            except Exception:
                out[e] = "unknown"; continue
            if code in (250, 251): out[e] = "valid"
            elif code in (550, 551, 552, 553, 501, 554): out[e] = "invalid"
            else: out[e] = "unknown"  # 4xx greylist/temp
        try: srv.quit()
        except Exception: pass
    except Exception:
        return {e: "unknown" for e in emails}
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--limit", type=int, default=8000)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--shard", type=int, default=0)
    a = ap.parse_args()
    conn = psycopg2.connect(**DB); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if a.calibrate:
        cur.execute("""SELECT id::text id, email, email_status AS hunter
                       FROM decisores_obra WHERE excluido_em IS NULL
                       AND COALESCE(email,'')<>'' AND email_status IS NOT NULL LIMIT %s""", (a.limit,))
    else:
        cur.execute("""SELECT id::text id, email, NULL AS hunter
                       FROM decisores_obra WHERE excluido_em IS NULL
                       AND COALESCE(email,'')<>'' AND email_smtp_status IS NULL LIMIT %s""", (a.limit,))
    rows = cur.fetchall()
    by_dom = defaultdict(list)
    syntax_bad = []
    for r in rows:
        if not RE_SYNTAX.match(r["email"] or ""): syntax_bad.append(r); continue
        dom = r["email"].split("@",1)[1].lower()
        # sharding por domínio (md5 estável) — cada worker pega um subconjunto disjunto
        if a.shards > 1 and int(hashlib.md5(dom.encode()).hexdigest(),16) % a.shards != a.shard:
            continue
        by_dom[dom].append(r)
    if a.shards > 1:
        syntax_bad = [r for r in syntax_bad if int(hashlib.md5((r["email"].split("@",1)[1].lower() if "@" in (r["email"] or "") else r["email"] or "x").encode()).hexdigest(),16) % a.shards == a.shard]
    print(f"Candidatos: {len(rows)} | domínios: {len(by_dom)} | sintaxe ruim: {len(syntax_bad)} | modo: {'CALIBRATE' if a.calibrate else 'RUN'} {'COMMIT' if a.commit else 'DRY'}")

    results = {}  # id -> status
    def writeback(pairs):
        if a.commit and pairs:
            for rid, st in pairs:
                cur.execute("UPDATE decisores_obra SET email_smtp_status=%s, email_smtp_em=now() WHERE id=%s::uuid", (st, rid))
            conn.commit()
    for r in syntax_bad: results[r["id"]] = "syntax"
    writeback([(r["id"], "syntax") for r in syntax_bad])
    done = 0
    for dom, rs in by_dom.items():
        res = probe_domain(dom, [r["email"] for r in rs])
        batch = []
        for r in rs:
            st = res.get(r["email"], "unknown"); results[r["id"]] = st; batch.append((r["id"], st))
        writeback(batch)  # grava incremental por domínio
        done += 1
        if done % 25 == 0: print(f"  ...{done}/{len(by_dom)} domínios", flush=True)
        time.sleep(0.15)

    from collections import Counter
    dist = Counter(results.values())
    print("Distribuição SMTP:", dict(dist))
    if a.calibrate:
        # matriz de concordância vs Hunter
        mat = defaultdict(lambda: Counter())
        hmap = {r["id"]: r["hunter"] for r in rows}
        for rid, st in results.items():
            mat[hmap[rid]][st] += 1
        print("\nGABARITO Hunter (linha) x SMTP (coluna):")
        for h, c in mat.items():
            print(f"  Hunter={h:11s} -> {dict(c)}")

if __name__ == "__main__":
    main()
