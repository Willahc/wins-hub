"""
dryrun_decisor_telefone_dossie.py — DRY-RUN (não grava nada).

Fluxo de telefone web com DOSSIÊ-PRIMEIRO + VALIDAÇÃO de identidade da página.

  1) Monta o dossiê: nome, cargo, empresa, e-mail (-> domínio), LinkedIn (-> slug).
  2) Busca ancorada no Serper: nome+empresa  E  o próprio e-mail (acha assinaturas
     / diretórios de contato).
  3) Abre o corpo das páginas em paralelo.
  4) Aceita CELULAR só se: (a) está a <= WINDOW chars do NOME, e (b) a PÁGINA é
     comprovadamente sobre a pessoa — contém e-mail, domínio ou slug do LinkedIn
     dele (âncora forte). DDD x UF e nº de fontes elevam a confiança.

Celular-only, anti-sintético. Sem Apify. Sem UPDATE.
"""
import argparse, os, re, sys, time, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, "/app")
import psycopg2
import requests

DB = {
    "host": os.getenv("DB_HOST", "db"), "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"), "password": os.getenv("DB_PASSWORD", ""),
}
SERPER = "https://google.serper.dev/search"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
WINDOW = 80
MAX_WORKERS = 4

PHONE_RE = re.compile(r"(?:\+?55[\s.\-]?)?\(?0?([1-9]\d)\)?[\s.\-]?(9\d{4})[\s.\-]?(\d{4})")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
LI_SLUG_RE = re.compile(r"/in/([A-Za-z0-9%\-_.]{3,})")
STOP = {"de", "da", "do", "dos", "das", "e", "of", "the", "ltda", "sa", "s/a", "epp"}
DDD_VALIDOS = {
    *range(11, 20), *range(21, 25), 27, 28, *range(31, 39), *range(41, 50),
    *range(51, 56), *range(61, 70), *range(71, 78), 79, *range(81, 90), *range(91, 100),
}
UF_DDD = {
    "SP": {11, 12, 13, 14, 15, 16, 17, 18, 19}, "RJ": {21, 22, 24}, "ES": {27, 28},
    "MG": {31, 32, 33, 34, 35, 37, 38}, "PR": {41, 42, 43, 44, 45, 46},
    "SC": {47, 48, 49}, "RS": {51, 53, 54, 55}, "DF": {61}, "GO": {62, 64},
    "TO": {63}, "MT": {65, 66}, "MS": {67}, "AC": {68}, "RO": {69},
    "BA": {71, 73, 74, 75, 77}, "SE": {79}, "PE": {81, 87}, "AL": {82},
    "PB": {83}, "RN": {84}, "CE": {85, 88}, "PI": {86, 89}, "PA": {91, 93, 94},
    "AM": {92, 97}, "RR": {95}, "AP": {96}, "MA": {98, 99},
}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s)


def comp_tokens(nome):
    return [t for t in norm(nome).split() if len(t) >= 3 and t not in STOP]


def serper(query, num=5):
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.post(SERPER, json={"q": query, "num": num},
                          headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code == 200 else []
    except requests.RequestException:
        return []


def fetch_body(url):
    if not url or "linkedin.com" in url or url.lower().endswith((".pdf", ".jpg", ".png", ".zip")):
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=8)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return ""
        return WS_RE.sub(" ", TAG_RE.sub(" ", r.text[:300_000]))
    except requests.RequestException:
        return ""


def name_pattern(nome):
    toks = comp_tokens(nome)
    if not toks:
        return None
    if len(toks) == 1:
        return re.compile(re.escape(toks[0]), re.IGNORECASE)
    return re.compile(re.escape(toks[0]) + r"(?:\s+\S+){0,2}\s+" + re.escape(toks[-1]), re.IGNORECASE)


def synthetic(a):
    return bool(re.search(r"(\d)\1{5,}", a)) or a in "0123456789" * 2 or a[-3:] in ("000",)


def phone_hits(text):
    out = []
    for m in PHONE_RE.finditer(text):
        ddd, p1, p2 = m.groups()
        if int(ddd) not in DDD_VALIDOS:
            continue
        a = p1 + p2
        if synthetic(a):
            continue
        out.append((m.start(), m.end(), f"+55{ddd}{a}", int(ddd)))
    return out


def gap(a, b):
    (s1, e1), (s2, e2) = a, b
    return max(0, s2 - e1) if e1 < s2 else (max(0, s1 - e2) if e2 < s1 else 0)


def ancora_forte(textlow, email, dom, slug):
    return (bool(email) and email in textlow) or (bool(dom) and dom in textlow) \
        or (bool(slug) and slug in textlow)


def buscar(nome, empresa, email, linkedin, uf):
    npat = name_pattern(nome)
    ufddd = UF_DDD.get((uf or "").upper(), set())
    email = (email or "").lower().strip()
    dom = email.split("@")[-1] if "@" in email else ""
    m = LI_SLUG_RE.search(linkedin or "")
    slug = norm(m.group(1)).replace(" ", "-") if m else ""
    agg = {}  # num -> dict(fontes,dist,ddd,ancora)

    queries = [f'"{nome}" "{empresa}" (telefone OR celular OR whatsapp OR contato)']
    if email:
        queries.append(f'"{email}"')

    for q in queries:
        results = serper(q, 5)
        bodies = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(fetch_body, r.get("link", "")): r.get("link", "") for r in results}
            for fut in as_completed(futs):
                bodies[futs[fut]] = fut.result()
        for r in results:
            link = r.get("link", "")
            blob = f"{r.get('title','')} {r.get('snippet','')} {bodies.get(link,'')}"
            if not blob.strip() or npat is None:
                continue
            low = blob.lower()
            forte = ancora_forte(low, email, dom, slug)
            nspans = [mm.span() for mm in npat.finditer(blob)]
            if not nspans:
                continue
            for ps, pe, num, ddd in phone_hits(blob):
                d = min(gap((ps, pe), ns) for ns in nspans)
                if d > WINDOW:
                    continue
                e = agg.setdefault(num, {"fontes": set(), "dist": 10**9, "ddd": ddd, "ancora": False})
                e["fontes"].add(link[:45])
                e["dist"] = min(e["dist"], d)
                e["ancora"] = e["ancora"] or forte
        if any(e["ancora"] and (e["ddd"] in ufddd or len(e["fontes"]) >= 2) for e in agg.values()):
            break

    def score(n):
        e = agg[n]
        return (e["ancora"], e["ddd"] in ufddd, len(e["fontes"]), -e["dist"])

    return [(n, agg[n]) for n in sorted(agg, key=score, reverse=True)]


def main():
    ap = argparse.ArgumentParser()
    global WINDOW
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--window", type=int, default=WINDOW)
    args = ap.parse_args()
    WINDOW = args.window

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute(r"""
      SELECT nome,cargo,empresa,email,linkedin_url,uf FROM (
        SELECT DISTINCT ON (d.nome) d.nome,d.cargo,o.empresa,d.email,d.linkedin_url,o.uf
        FROM decisores_obra d JOIN obras o ON o.id=d.obra_id
        WHERE o.classificacao_computed IN('OURO','PRATA') AND o.visivel
          AND d.excluido_em IS NULL AND COALESCE(d.telefone,'')=''
          AND (COALESCE(d.linkedin_url,'')<>'' OR COALESCE(d.email,'')<>'')
          AND d.nome NOT ILIKE 'Contato Comercial%%' AND d.nome NOT ILIKE 'Equipe %%'
          AND o.empresa NOT ILIKE 'MUNIC%%'
          AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
        ORDER BY d.nome
      ) s ORDER BY md5(nome) LIMIT %s
    """, (args.limit,))
    cands = cur.fetchall()
    print(f"\n=== DRY-RUN telefone dossiê+validação | {len(cands)} decisores | NADA sera gravado ===\n")

    n_verde = n_amarelo = 0
    t0 = time.time()
    for i, (nome, cargo, empresa, email, linkedin, uf) in enumerate(cands, 1):
        dom = (email.split("@")[-1] if email and "@" in email else "—")
        print(f"[{i:02d}] {nome[:24]:24} {uf or '--':2} | {empresa[:20]:20} | dom:{dom[:16]:16} li:{'s' if linkedin else 'n'}")
        res = buscar(nome, empresa, email, linkedin, uf)
        if not res:
            print(f"      └─ sem celular atribuível")
            continue
        num, e = res[0]
        ddmatch = e["ddd"] in UF_DDD.get((uf or "").upper(), set())
        verde = e["ancora"] and (ddmatch or len(e["fontes"]) >= 2)
        flags = ("âncora✓ " if e["ancora"] else "âncora✗ ") + ("DDD✓" if ddmatch else "DDD✗")
        if verde:
            n_verde += 1
            print(f"      └─ 🟢 {num} ({len(e['fontes'])}f,{e['dist']}c,{flags})")
        else:
            n_amarelo += 1
            print(f"      └─ 🟡 {num} ({len(e['fontes'])}f,{e['dist']}c,{flags})")

    tot = len(cands)
    print(f"\n=== 🟢 alta conf: {n_verde}/{tot} | 🟡 fraco: {n_amarelo}/{tot} | {(time.time()-t0):.0f}s ===")
    print("    🟢 = celular perto do nome + página com e-mail/domínio/LinkedIn dele")


if __name__ == "__main__":
    main()
