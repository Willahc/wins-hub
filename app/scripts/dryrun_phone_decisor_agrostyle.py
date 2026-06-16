"""
dryrun_phone_decisor_agrostyle.py — DRY-RUN v2 (não grava nada).

Fluxo "Agro-style" otimizado:
  Serper -> abre CORPO das top páginas EM PARALELO -> aceita telefone só perto
  do NOME (proximidade <= WINDOW) -> celular-only -> extrai também links wa.me
  -> pontua DDD x UF da obra -> anti-sintético -> early-exit em hit confiável.

Sem phonenumbers (regex BR). Sem Apify (páginas institucionais abrem com requests).
Sem UPDATE.
"""
import argparse, os, re, sys, time
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
WINDOW = 80          # distância máxima nome <-> telefone (chars)
MAX_WORKERS = 4      # fetches paralelos (I/O; leve pro 1vCPU)

PHONE_RE = re.compile(r"(?:\+?55[\s.\-]?)?\(?0?([1-9]\d)\)?[\s.\-]?(\d{4,5})[\s.\-]?(\d{4})")
WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send\?phone=)(\d{10,13})", re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
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
    toks = [t for t in re.split(r"\s+", nome.strip()) if len(t) >= 3]
    if not toks:
        return None
    if len(toks) == 1:
        return re.compile(re.escape(toks[0]), re.IGNORECASE)
    return re.compile(re.escape(toks[0]) + r"(?:\s+\S+){0,2}\s+" + re.escape(toks[-1]), re.IGNORECASE)


def synthetic(assinante):
    if re.search(r"(\d)\1{5,}", assinante):           # 6+ dígitos repetidos
        return True
    if assinante in "0123456789" * 2 or assinante in "9876543210" * 2:  # sequencial
        return True
    return False


def phone_hits(text):
    """[(start, end, num, ddd)] de CELULARES BR plausíveis."""
    out = []
    for m in PHONE_RE.finditer(text):
        ddd, p1, p2 = m.groups()
        if int(ddd) not in DDD_VALIDOS:
            continue
        assinante = p1 + p2
        if len(assinante) != 9 or assinante[0] != "9" or synthetic(assinante):
            continue
        out.append((m.start(), m.end(), f"+55{ddd}{assinante}", int(ddd)))
    return out


def wa_hits(text):
    """[(start, end, num, ddd)] de links wa.me / send?phone=."""
    out = []
    for m in WA_RE.finditer(text):
        d = m.group(1)
        if d.startswith("55"):
            d = d[2:]
        if len(d) == 11 and d[2] == "9":   # DDD + 9 + 8
            out.append((m.start(), m.end(), f"+55{d}", int(d[:2])))
    return out


def gap(a, b):
    (s1, e1), (s2, e2) = a, b
    if e1 < s2:
        return s2 - e1
    if e2 < s1:
        return s1 - e2
    return 0


def near_name(text, npat, hits):
    """{num: (ddd, mindist)} para hits a <= WINDOW de uma ocorrência do nome."""
    if npat is None:
        return {}
    nspans = [m.span() for m in npat.finditer(text)]
    if not nspans:
        return {}
    res = {}
    for ps, pe, num, ddd in hits:
        d = min(gap((ps, pe), ns) for ns in nspans)
        if d <= WINDOW and (num not in res or d < res[num][1]):
            res[num] = (ddd, d)
    return res


def buscar(nome, empresa, uf):
    npat = name_pattern(nome)
    ufddd = UF_DDD.get((uf or "").upper(), set())
    agg = {}  # num -> dict(fontes:set, dist:int, ddd:int, wa:bool)

    def absorve(blob, origem, link, is_wa):
        hits = wa_hits(blob) if is_wa else phone_hits(blob)
        for num, (ddd, d) in near_name(blob, npat, hits).items():
            e = agg.setdefault(num, {"fontes": set(), "dist": 10**9, "ddd": ddd, "wa": False})
            e["fontes"].add(origem + ":" + link[:40])
            e["dist"] = min(e["dist"], d)
            e["wa"] = e["wa"] or is_wa

    queries = [
        f'"{nome}" "{empresa}" (whatsapp OR celular OR telefone OR contato)',
        f'"{nome}" "{empresa}" (sócio OR currículo OR palestrante OR "fale conosco")',
    ]
    for q in queries:
        results = serper(q, 5)
        bodies = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(fetch_body, r.get("link", "")): r.get("link", "") for r in results}
            for fut in as_completed(futs):
                bodies[futs[fut]] = fut.result()
        for r in results:
            link = r.get("link", "")
            snippet = f"{r.get('title','')} {r.get('snippet','')}"
            for blob, origem in ((snippet, "snip"), (bodies.get(link, ""), "body")):
                if not blob:
                    continue
                absorve(blob, origem, link, is_wa=True)
                absorve(blob, origem, link, is_wa=False)
        # early-exit: já temos hit forte?
        if any(e["wa"] or (len(e["fontes"]) >= 2 and e["ddd"] in ufddd and e["dist"] <= 30)
               for e in agg.values()):
            break

    def score(num):
        e = agg[num]
        return (e["wa"], e["ddd"] in ufddd, len(e["fontes"]), -e["dist"])

    out = []
    for num in sorted(agg, key=score, reverse=True):
        e = agg[num]
        out.append((num, len(e["fontes"]), e["dist"], e["wa"], e["ddd"] in ufddd))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute(r"""
        SELECT DISTINCT ON (d.nome) d.nome, o.empresa, o.classificacao_computed, o.uf
        FROM decisores_obra d JOIN obras o ON o.id=d.obra_id
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND d.excluido_em IS NULL AND COALESCE(d.telefone,'')=''
          AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo NOT IN ('','OUTRO')
          AND d.nome NOT ILIKE 'Contato Comercial%%' AND d.nome NOT ILIKE 'Equipe %%'
          AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
          AND COALESCE(o.empresa,'')<>''
        ORDER BY d.nome
        LIMIT %s
    """, (args.limit,))
    cands = cur.fetchall()
    print(f"\n=== DRY-RUN v2 (janela {WINDOW}c, celular-only) | {len(cands)} decisores | NADA sera gravado ===\n")

    hits = 0
    t0 = time.time()
    for i, (nome, empresa, tier, uf) in enumerate(cands, 1):
        res = buscar(nome, empresa, uf)
        if res:
            hits += 1
            num, nsrc, d, wa, ddmatch = res[0]
            flags = ("WA " if wa else "") + ("DDD✓" if ddmatch else "DDD✗")
            conf = "🟢" if (wa or (nsrc >= 2 and ddmatch)) else "🟡"
            print(f"[{i:02d}/{len(cands)}] {conf} {nome[:24]:24} {tier:5} {uf or '--':2} | {num} ({nsrc}f,{d}c,{flags})")
        else:
            print(f"[{i:02d}/{len(cands)}] -- {nome[:24]:24} {tier:5} {uf or '--':2} | {empresa[:30]}")

    print(f"\n=== hit rate: {hits}/{len(cands)} ({100*hits/max(len(cands),1):.0f}%) | {(time.time()-t0):.0f}s | 🟢=alta conf ===")


if __name__ == "__main__":
    main()
