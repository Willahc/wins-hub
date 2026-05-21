"""
enrich_via_scrape_haiku.py — Pipeline completo: subdomínios → scrape → Haiku extract.

Steps:
  1. Pra cada empresa OURO/PRATA alvo (sem email OU sem telefone):
     a. HackerTarget API → lista subdomínios
     b. Filtra subdomínios departamentais (suprimentos/compras/fornecedor/contato/vendas)
     c. Adiciona URL raiz (https://dominio)
  2. Pra cada URL alvo: HTTP GET (curl-like), 60KB primeiros
  3. Haiku 4.5 extrai emails (@dominio principal) + telefones (formato BR)
  4. UPDATE obras.nivel1_email/telefone via cross-match nome ou fallback genérico

Dry-run --commit pra escrever.
"""
import argparse, asyncio, json, os, re, sys, time
from collections import defaultdict
sys.path.insert(0, "/app")
import anthropic
import phonenumbers
import psycopg2
import requests

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}
MODEL = "claude-haiku-4-5-20251001"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WinsHubBot/1.0)"}

DEPT_KEYS = ("suprimentos","compras","fornecedor","contato","vendas","procurement",
             "supply","sac","comercial","fornecermais","institucional")

EMAIL_BLACKLIST = ("noreply","no-reply","dpo","lgpd","ouvidoria","imprensa","rh@","trainee","vagas")
GENERIC_PRIORITY = ("fornecedores","compras","suprimentos","comercial","atendimento","contato","sac","info")


def hackertarget(dominio):
    """Free 50/dia. Retorna lista de subdomínios completos."""
    try:
        r = requests.get(f"https://api.hackertarget.com/hostsearch/?q={dominio}",
                         timeout=20, headers=HEADERS)
        if r.status_code != 200 or "api count exceeded" in r.text.lower():
            return []
        return [line.split(",")[0].strip().lower() for line in r.text.splitlines() if line.strip()]
    except requests.RequestException:
        return []


def priorizar_urls(subdominios, dominio_raiz):
    """Filtra subdomínios departamentais + adiciona raiz. Max 4 URLs."""
    deps = [s for s in subdominios if any(k in s for k in DEPT_KEYS)]
    deps = sorted(deps, key=lambda s: (-sum(k in s for k in DEPT_KEYS), len(s)))[:3]
    urls = [f"https://{s}" for s in deps]
    urls.append(f"https://www.{dominio_raiz}")
    return urls[:4]


def fetch_url(url, max_bytes=60000):
    try:
        r = requests.get(url, timeout=20, headers=HEADERS, allow_redirects=True)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type","").lower()
        if "html" not in ctype and "text" not in ctype:
            return None
        text = r.text[:max_bytes]
        # strip scripts/styles pra reduzir noise
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL|re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL|re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()[:30000]
    except requests.RequestException:
        return None


_client = anthropic.Anthropic()
SYSTEM = """Auditor de páginas corporativas. Extrai emails (@dominio principal) e telefones (BR).
Critérios:
- Email: apenas no domínio principal informado (não outros)
- Telefone: formato BR (DDD + 8/9 dígitos), descartar 0800/celular pessoal/internacional
- Identificar setor se possível (compras/suprimentos/comercial/sac/geral)
- Descartar: noreply, dpo, lgpd, vagas, trainee

Responda JSON: {"emails":[{"email":"x@y","setor":"compras|geral|outro"}], "telefones":[{"numero":"(21)1234-5678","setor":"geral"}]}
Sem markdown, sem texto extra."""


def haiku_extract(texto, dominio_principal):
    user = f"Domínio principal: {dominio_principal}\n\nTexto da página:\n{texto[:25000]}"
    try:
        resp = _client.messages.create(
            model=MODEL, max_tokens=600, system=SYSTEM,
            messages=[{"role":"user","content":user}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*","",raw); raw = re.sub(r"\s*```$","",raw)
        return json.loads(raw)
    except Exception as e:
        return {"emails":[], "telefones":[], "erro":str(e)[:80]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT o.empresa, ed.dominio
        FROM obras o
        JOIN empresa_dominios ed ON regexp_replace(ed.cnpj,'[^0-9]','','g') = regexp_replace(COALESCE(o.cnpj,''),'[^0-9]','','g')
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND COALESCE(ed.dominio,'') <> ''
          AND (COALESCE(o.nivel1_email,'') = '' OR COALESCE(o.nivel1_telefone,'') = '')
        ORDER BY o.empresa
    """)
    targets = cur.fetchall()
    if args.limit: targets = targets[:args.limit]
    print(f"\nScrape pipeline: {len(targets)} (empresa,dominio) targets\n")

    if not args.commit:
        for e,d in targets[:10]: print(f"  {e[:40]:40} @ {d}")
        return

    total_emails = total_tels = total_obras_email = total_obras_tel = 0
    t0 = time.time()
    for i, (empresa, dominio) in enumerate(targets, 1):
        subs = hackertarget(dominio)
        urls = priorizar_urls(subs, dominio)
        print(f"[{i:02d}/{len(targets)}] {empresa[:30]:30} dom={dominio:22} subs={len(subs):3} urls={len(urls)}")

        emails_all = []
        tels_all = []
        for url in urls:
            html = fetch_url(url)
            if not html: continue
            extracted = haiku_extract(html, dominio)
            for em in extracted.get("emails",[]):
                e_low = (em.get("email") or "").lower()
                if not e_low.endswith("@"+dominio): continue
                if any(b in e_low for b in EMAIL_BLACKLIST): continue
                emails_all.append({"email": e_low, "setor": (em.get("setor") or "").lower(), "url": url})
            for tel in extracted.get("telefones",[]):
                num = (tel.get("numero") or "").strip()
                try:
                    p = phonenumbers.parse(num, "BR")
                    if phonenumbers.is_valid_number(p):
                        tels_all.append({
                            "e164": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164),
                            "national": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL),
                            "setor": (tel.get("setor") or "").lower(),
                            "url": url,
                        })
                except phonenumbers.NumberParseException:
                    continue
            time.sleep(0.5)

        # Dedup
        emails_dedup = {e["email"]: e for e in emails_all}.values()
        tels_dedup = {t["e164"]: t for t in tels_all}.values()
        total_emails += len(list(emails_dedup))
        total_tels += len(list(tels_dedup))

        # Priorizar genéricos B2B
        def pick_email():
            for keypref in GENERIC_PRIORITY:
                for e in emails_dedup:
                    if e["email"].startswith(keypref+"@"): return e["email"]
            for e in emails_dedup:
                if e["setor"] in ("compras","suprimentos","comercial","geral"): return e["email"]
            return None
        def pick_tel():
            for t in tels_dedup:
                if t["setor"] in ("compras","suprimentos","comercial","geral"): return t
            return next(iter(tels_dedup), None)

        em_best = pick_email()
        tel_best = pick_tel()

        # UPDATE obras da empresa (fallback genérico)
        cur.execute("""
            SELECT id::text FROM obras
            WHERE classificacao_computed IN ('OURO','PRATA') AND visivel AND empresa = %s
        """, (empresa,))
        obra_ids = [r[0] for r in cur.fetchall()]
        for oid in obra_ids:
            sets = []; params = []
            if em_best:
                sets.append("nivel1_email = COALESCE(NULLIF(nivel1_email,''), %s)")
                params.append(em_best)
            if tel_best:
                sets.append("nivel1_telefone = COALESCE(NULLIF(nivel1_telefone,''), %s)")
                sets.append("nivel1_telefone_e164 = COALESCE(NULLIF(nivel1_telefone_e164,''), %s)")
                sets.append("nivel1_telefone_status = COALESCE(NULLIF(nivel1_telefone_status,''), 'valido')")
                params += [tel_best["national"], tel_best["e164"]]
            if sets:
                params.append(oid)
                cur.execute(f"UPDATE obras SET {', '.join(sets)} WHERE id = %s", params)
                if cur.rowcount:
                    if em_best: total_obras_email += 1
                    if tel_best: total_obras_tel += 1
        conn.commit()
        print(f"   emails_uniq={len(set(e['email'] for e in emails_all))} tels_uniq={len(set(t['e164'] for t in tels_all))} pick_em={em_best or '-'} pick_tel={(tel_best or {}).get('national','-')}")

    print(f"\n{'='*60}")
    print(f"  Emails coletados (bruto): {total_emails}")
    print(f"  Telefones coletados (bruto): {total_tels}")
    print(f"  Obras com email novo: {total_obras_email}")
    print(f"  Obras com telefone novo: {total_obras_tel}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
