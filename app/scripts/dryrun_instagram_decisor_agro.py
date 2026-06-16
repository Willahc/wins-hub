"""
dryrun_instagram_decisor_agro.py — DRY-RUN (não grava nada).

Estratégia do Agro aplicada a DECISOR de obra:
  1) Serper acha o @ do Instagram do decisor
  2) Apify (instagram-profile-scraper) abre o perfil e lê a BIO + contatos
  3) extrai wa.me / telefone / e-mail da bio

Requer APIFY_TOKEN no ambiente. Sem UPDATE.
"""
import argparse, os, re, sys, time
sys.path.insert(0, "/app")
import psycopg2
import requests

DB = {
    "host": os.getenv("DB_HOST", "db"), "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"), "password": os.getenv("DB_PASSWORD", ""),
}
SERPER = "https://google.serper.dev/search"
APIFY_ACTOR = "apify~instagram-profile-scraper"
APIFY_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"

WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send\?phone=)(\d{10,13})", re.I)
PHONE_RE = re.compile(r"(?:\+?55[\s.\-]?)?\(?0?([1-9]\d)\)?[\s.\-]?(9\d{4})[\s.\-]?(\d{4})")
EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w{2,}")
IG_LINK_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?|$|/)")
IG_SKIP = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about"}


def serper(query, num=6):
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.post(SERPER, json={"q": query, "num": num},
                          headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code == 200 else []
    except requests.RequestException:
        return []


def achar_handle(nome, empresa):
    """Acha @ do Instagram via Serper (etapa 1 do Agro)."""
    for q in (f'site:instagram.com "{nome}" {empresa}', f'{nome} {empresa} instagram'):
        for r in serper(q, 6):
            m = IG_LINK_RE.search(r.get("link", ""))
            if m and m.group(1).lower() not in IG_SKIP:
                return m.group(1)
        time.sleep(0.2)
    return None


def apify_profile(handle, token):
    """Etapa 2 do Agro: abre o perfil no Apify e devolve o item bruto."""
    try:
        r = requests.post(f"{APIFY_URL}?token={token}",
                          json={"usernames": [handle]}, timeout=120)
        if r.status_code not in (200, 201):
            return {"_erro": f"http {r.status_code}"}
        items = r.json()
        return items[0] if items else {}
    except requests.RequestException as e:
        return {"_erro": str(e)[:60]}


def extrair_contatos(item):
    """Etapa 3: wa.me / telefone / e-mail da bio + campos business."""
    bio = " ".join(str(item.get(k, "")) for k in
                   ("biography", "externalUrl", "businessPhoneNumber",
                    "businessEmail", "publicPhoneNumber", "publicEmail"))
    wa = [f"+55{d[2:] if d.startswith('55') else d}" for d in WA_RE.findall(bio)]
    tel = [f"+55{ddd}{a}{b}" for ddd, a, b in PHONE_RE.findall(bio)]
    email = EMAIL_RE.findall(bio)
    return wa, tel, email, bio.strip()[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        print("\n!! APIFY_TOKEN ausente — defina no ambiente para rodar a etapa Apify.\n")

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute(r"""
        SELECT nome, empresa, tier FROM (
          SELECT DISTINCT ON (d.nome) d.nome, o.empresa, o.classificacao_computed AS tier
          FROM decisores_obra d JOIN obras o ON o.id=d.obra_id
          WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
            AND d.excluido_em IS NULL AND COALESCE(d.telefone,'')=''
            AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo NOT IN ('','OUTRO')
            AND d.nome NOT ILIKE 'Contato Comercial%%' AND d.nome NOT ILIKE 'Equipe %%'
            AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
            AND COALESCE(o.empresa,'')<>''
          ORDER BY d.nome
        ) s ORDER BY md5(nome) LIMIT %s
    """, (args.limit,))
    cands = cur.fetchall()
    print(f"\n=== DRY-RUN Agro/Instagram | {len(cands)} decisores | NADA sera gravado ===\n")

    n_ig = n_contato = 0
    t0 = time.time()
    for i, (nome, empresa, tier) in enumerate(cands, 1):
        handle = achar_handle(nome, empresa)
        if not handle:
            print(f"[{i:02d}/{len(cands)}] --  {nome[:26]:26} {tier:5} | sem Instagram")
            continue
        n_ig += 1
        if not token:
            print(f"[{i:02d}/{len(cands)}] @   {nome[:26]:26} {tier:5} | @{handle} (Apify pendente: sem token)")
            continue
        item = apify_profile(handle, token)
        if item.get("_erro"):
            print(f"[{i:02d}/{len(cands)}] @!  {nome[:26]:26} {tier:5} | @{handle} (apify: {item['_erro']})")
            continue
        wa, tel, email, bio = extrair_contatos(item)
        if wa or tel or email:
            n_contato += 1
            achados = " ".join(filter(None, [
                ("WA " + wa[0]) if wa else "",
                ("tel " + tel[0]) if tel else "",
                ("@mail " + email[0]) if email else ""]))
            print(f"[{i:02d}/{len(cands)}] 🟢  {nome[:26]:26} {tier:5} | @{handle} | {achados}")
        else:
            print(f"[{i:02d}/{len(cands)}] @-  {nome[:26]:26} {tier:5} | @{handle} | bio sem contato: {bio[:50]}")
        time.sleep(0.3)

    tot = len(cands)
    print(f"\n=== Instagram achado: {n_ig}/{tot} | com contato na bio: {n_contato}/{tot} | {(time.time()-t0):.0f}s ===")


if __name__ == "__main__":
    main()
