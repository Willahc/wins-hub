"""
dryrun_decisor_dossie_busca.py — DRY-RUN (não grava nada).

Princípio: DOSSIÊ PRIMEIRO, busca depois, com VALIDAÇÃO de identidade.
  1) Junta tudo que já temos do decisor: nome, cargo, empresa, e-mail (-> domínio),
     LinkedIn.
  2) Só então busca o Instagram via Serper (ancorado no nome + empresa).
  3) Apify abre o perfil e a bio.
  4) VALIDA: o fullName do perfil tem que casar com o nome conhecido do decisor.
     Sem casar -> descarta (corta prefeitura/portal/empresa).
  5) Extrai wa.me / telefone / e-mail só de perfil validado.

Requer APIFY_TOKEN. Sem UPDATE.
"""
import argparse, os, re, sys, time, unicodedata
sys.path.insert(0, "/app")
import psycopg2
import requests

DB = {
    "host": os.getenv("DB_HOST", "db"), "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"), "password": os.getenv("DB_PASSWORD", ""),
}
SERPER = "https://google.serper.dev/search"
APIFY_URL = "https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run-sync-get-dataset-items"

WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send\?phone=)(\d{10,13})", re.I)
PHONE_RE = re.compile(r"(?:\+?55[\s.\-]?)?\(?0?([1-9]\d)\)?[\s.\-]?(9\d{4})[\s.\-]?(\d{4})")
EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w{2,}")
IG_LINK_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?|$|/)")
IG_SKIP = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about"}
STOP = {"de", "da", "do", "dos", "das", "e", "of", "the", "jr", "junior", "neto", "filho"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s)


def tokens(nome):
    return {t for t in norm(nome).split() if len(t) >= 3 and t not in STOP}


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


def achar_handles(nome, empresa, maxn=2):
    """Candidatos a @ (etapa busca, ancorada em nome+empresa)."""
    out = []
    for q in (f'site:instagram.com "{nome}" {empresa}', f'"{nome}" instagram'):
        for r in serper(q, 6):
            m = IG_LINK_RE.search(r.get("link", ""))
            if m:
                h = m.group(1)
                if h.lower() not in IG_SKIP and h not in out:
                    out.append(h)
            if len(out) >= maxn:
                return out
        time.sleep(0.2)
    return out


def apify_profile(handle, token):
    try:
        r = requests.post(f"{APIFY_URL}?token={token}", json={"usernames": [handle]}, timeout=120)
        if r.status_code not in (200, 201):
            return {"_erro": f"http {r.status_code}"}
        items = r.json()
        return items[0] if items else {}
    except requests.RequestException as e:
        return {"_erro": str(e)[:50]}


def valida_identidade(nome_decisor, handle, item):
    """fullName do perfil casa com o nome conhecido? (>=2 tokens) ou handle contém nome."""
    dt = tokens(nome_decisor)
    full = item.get("fullName") or item.get("username") or ""
    overlap = len(dt & tokens(full))
    if overlap >= 2:
        return True, full
    hnorm = norm(handle).replace(" ", "")
    if overlap >= 1 and any(t in hnorm for t in dt if len(t) >= 4):
        return True, full
    return False, full


def extrair(item):
    bio = " ".join(str(item.get(k, "")) for k in
                   ("biography", "externalUrl", "businessPhoneNumber", "businessEmail",
                    "publicPhoneNumber", "publicEmail"))
    wa = [f"+55{d[2:] if d.startswith('55') else d}" for d in WA_RE.findall(bio)]
    tel = [f"+55{ddd}{a}{b}" for ddd, a, b in PHONE_RE.findall(bio)]
    return wa, tel, EMAIL_RE.findall(bio), bio.strip()[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    token = os.getenv("APIFY_TOKEN", "").strip()

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute(r"""
      SELECT nome,cargo,empresa,email,linkedin_url FROM (
        SELECT DISTINCT ON (d.nome) d.nome, d.cargo, o.empresa, d.email, d.linkedin_url
        FROM decisores_obra d JOIN obras o ON o.id=d.obra_id
        WHERE o.classificacao_computed IN('OURO','PRATA') AND o.visivel
          AND d.excluido_em IS NULL AND COALESCE(d.telefone,'')=''
          AND (COALESCE(d.linkedin_url,'')<>'' OR COALESCE(d.email,'')<>'')
          AND d.nome NOT ILIKE 'Contato Comercial%%' AND d.nome NOT ILIKE 'Equipe %%'
          AND d.linkedin_url NOT ILIKE '%%/contato-comercial%%'
          AND o.empresa NOT ILIKE 'MUNIC%%'
          AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
        ORDER BY d.nome
      ) s ORDER BY md5(nome) LIMIT %s
    """, (args.limit,))
    cands = cur.fetchall()
    print(f"\n=== DRY-RUN dossiê+busca | {len(cands)} decisores | NADA sera gravado ===\n")

    n_hit = 0
    t0 = time.time()
    for i, (nome, cargo, empresa, email, linkedin) in enumerate(cands, 1):
        dom = email.split("@")[-1] if email and "@" in email else "—"
        print(f"[{i:02d}] {nome[:24]:24} | {empresa[:22]:22} | dom:{dom[:18]:18} | li:{'sim' if linkedin else 'não'}")
        handles = achar_handles(nome, empresa)
        if not handles:
            print(f"      └─ sem Instagram localizável")
            continue
        achou = False
        for h in handles:
            if not token:
                print(f"      └─ @{h} (Apify pendente: sem token)")
                achou = True
                break
            item = apify_profile(h, token)
            if item.get("_erro"):
                print(f"      └─ @{h} apify:{item['_erro']}")
                continue
            ok, full = valida_identidade(nome, h, item)
            if not ok:
                print(f"      └─ @{h} ✗ não é a pessoa (perfil='{full[:30]}')")
                continue
            wa, tel, mail, bio = extrair(item)
            achou = True
            if wa or tel:
                n_hit += 1
                got = " ".join(filter(None, [("WA " + wa[0]) if wa else "", ("tel " + tel[0]) if tel else ""]))
                print(f"      └─ @{h} ✓ {full[:24]} | 🟢 {got}")
            else:
                print(f"      └─ @{h} ✓ {full[:24]} | sem fone na bio: {bio[:45]}")
            break
        time.sleep(0.3)

    print(f"\n=== telefone/WA na bio (perfil validado): {n_hit}/{len(cands)} | {(time.time()-t0):.0f}s ===")


if __name__ == "__main__":
    main()
