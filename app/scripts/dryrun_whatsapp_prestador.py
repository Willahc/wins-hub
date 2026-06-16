"""
dryrun_whatsapp_prestador.py — DRY-RUN (não grava nada).

Mesmo motor do fluxo Agro, aplicado onde o WhatsApp REALMENTE é público:
prestador pequeno/local. Alvo = WhatsApp da EMPRESA.

  Serper -> abre CORPO das páginas em paralelo -> extrai links wa.me /
  send?phone= e celulares -> aceita se a PÁGINA menciona a empresa
  (atribuição por nome da empresa, não por pessoa).

Sem Apify (site/Google Business abrem com requests). Sem phonenumbers. Sem UPDATE.
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
WINDOW = 200
MAX_WORKERS = 4

PHONE_RE = re.compile(r"(?:\+?55[\s.\-]?)?\(?0?([1-9]\d)\)?[\s.\-]?(\d{4,5})[\s.\-]?(\d{4})")
WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send\?phone=)(\d{10,13})", re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
STOP = {"LTDA", "ME", "EPP", "SA", "S/A", "EIRELI", "DO", "DE", "DA", "DOS", "DAS",
        "E", "&", "EM", "COM", "MEI", "CIA"}
DDD_VALIDOS = {
    *range(11, 20), *range(21, 25), 27, 28, *range(31, 39), *range(41, 50),
    *range(51, 56), *range(61, 70), *range(71, 78), 79, *range(81, 90), *range(91, 100),
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


def comp_tokens(nome):
    toks = [t for t in re.split(r"[\s.,]+", nome.upper()) if len(t) >= 3 and t not in STOP]
    return toks[:3]


def mentions(text, toks):
    if not toks:
        return False
    up = text.upper()
    present = sum(1 for t in toks if t in up)
    return present >= min(2, len(toks))


def synthetic(assinante):
    return bool(re.search(r"(\d)\1{5,}", assinante)) or assinante in "0123456789" * 2


def phone_hits(text):
    out = []
    for m in PHONE_RE.finditer(text):
        ddd, p1, p2 = m.groups()
        if int(ddd) not in DDD_VALIDOS:
            continue
        a = p1 + p2
        if len(a) != 9 or a[0] != "9" or synthetic(a):
            continue
        out.append(f"+55{ddd}{a}")
    return out


def wa_hits(text):
    out = []
    for m in WA_RE.finditer(text):
        d = m.group(1)
        if d.startswith("55"):
            d = d[2:]
        if len(d) == 11 and d[2] == "9":
            out.append(f"+55{d}")
    return out


def buscar(nome, municipio, uf):
    toks = comp_tokens(nome)
    wa, cel = {}, {}  # num -> set(fontes)
    queries = [
        f'"{nome}" {municipio} (whatsapp OR contato OR telefone)',
        f'{nome} {municipio} {uf} (instagram OR "wa.me" OR site)',
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
            for blob in (snippet, bodies.get(link, "")):
                if not blob or not mentions(blob, toks):
                    continue
                for num in wa_hits(blob):
                    wa.setdefault(num, set()).add(link[:45])
                for num in phone_hits(blob):
                    cel.setdefault(num, set()).add(link[:45])
        if wa:  # achou WhatsApp explícito -> early-exit
            break
    wa_rank = sorted(wa.items(), key=lambda kv: len(kv[1]), reverse=True)
    cel_rank = sorted(cel.items(), key=lambda kv: len(kv[1]), reverse=True)
    return wa_rank, cel_rank


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--uf", default="SP")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(NULLIF(nome_fantasia,''), razao_social), municipio_nome, uf
        FROM fornecedores
        WHERE porte_inferido='PEQUENA' AND uf=%s AND cnae_principal LIKE '43%%'
          AND COALESCE(razao_social,'')<>'' AND COALESCE(municipio_nome,'')<>''
        ORDER BY md5(cnpj)
        LIMIT %s
    """, (args.uf, args.limit))
    cands = cur.fetchall()
    print(f"\n=== DRY-RUN wa.me prestador | {len(cands)} empresas {args.uf} | NADA sera gravado ===\n")

    wa_hits_n = cel_hits_n = 0
    t0 = time.time()
    for i, (nome, mun, uf) in enumerate(cands, 1):
        wa, cel = buscar(nome, mun, uf)
        if wa:
            wa_hits_n += 1
            num, srcs = wa[0]
            print(f"[{i:02d}/{len(cands)}] 🟢 WA  {nome[:30]:30} {mun[:14]:14} | {num} ({len(srcs)}f)")
        elif cel:
            cel_hits_n += 1
            num, srcs = cel[0]
            print(f"[{i:02d}/{len(cands)}] 🟡 cel {nome[:30]:30} {mun[:14]:14} | {num} ({len(srcs)}f)")
        else:
            print(f"[{i:02d}/{len(cands)}] --     {nome[:30]:30} {mun[:14]:14} |")

    tot = len(cands)
    print(f"\n=== wa.me: {wa_hits_n}/{tot} ({100*wa_hits_n/max(tot,1):.0f}%) | "
          f"+celular: {cel_hits_n} | qualquer contato: {wa_hits_n+cel_hits_n}/{tot} "
          f"({100*(wa_hits_n+cel_hits_n)/max(tot,1):.0f}%) | {(time.time()-t0):.0f}s ===")


if __name__ == "__main__":
    main()
