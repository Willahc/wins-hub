"""
harvest_emails_via_pdf.py — Email harvest via PDFs corporativos.
Política de Compras / Manual Fornecedores / Código de Conduta listam emails reais.

Fluxo:
  1. Pra cada (empresa, dominio) com gap email
  2. Serper: `"@dominio" filetype:pdf (fornecedor OR compras OR conduta OR contato)`
  3. Fetch top 3 PDFs (limit 5MB)
  4. pypdf extrai texto → regex emails @dominio + phones BR
  5. UPDATE obras.nivel1_email se vazio + cross-match nome ou genérico B2B
"""
import argparse, io, json, os, re, sys, time
from collections import defaultdict
sys.path.insert(0, "/app")
import phonenumbers
import psycopg2
import pypdf
import requests

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}
SERPER = "https://google.serper.dev/search"
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+")
EMAIL_BL = ("noreply@","no-reply@","dpo@","lgpd@","ouvidoria@","imprensa@","rh@","trainee@","vagas@")
GENERIC_B2B = ("fornecedores@","fornecedor@","compras@","suprimentos@","comercial@","contato@","atendimento@","sac@")
PHONE_RE = re.compile(r"(?:\+?55[\s.-]?)?\(?0?(\d{2})\)?[\s.-]?(\d{4,5})[\s.-]?(\d{4})")

def serper(query, num=5):
    key = os.getenv("SERPER_API_KEY","").strip()
    if not key: return []
    try:
        r = requests.post(SERPER, json={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type":"application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code==200 else []
    except requests.RequestException: return []

def fetch_pdf(url, max_mb=5):
    try:
        r = requests.get(url, timeout=30, stream=True,
                         headers={"User-Agent":"Mozilla/5.0 (compatible; WinsHubBot/1.0)"})
        if r.status_code != 200: return None
        content = b""
        for chunk in r.iter_content(1024*64):
            content += chunk
            if len(content) > max_mb*1024*1024:
                return None
        return content
    except requests.RequestException: return None

def extract_pdf_text(pdf_bytes, max_chars=80000):
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages[:50]:  # cap 50 pages
            try: text += page.extract_text() + "\n"
            except Exception: continue
            if len(text) > max_chars: break
        return text[:max_chars]
    except Exception: return ""

def parse_phones(texto):
    phones = set()
    for ddd, p1, p2 in PHONE_RE.findall(texto):
        num = f"{ddd}{p1}{p2}"
        try:
            p = phonenumbers.parse(num, "BR")
            if phonenumbers.is_valid_number(p):
                e164 = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
                if not e164.startswith("+550800"):
                    phones.add(e164)
        except phonenumbers.NumberParseException: continue
    return phones

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
          AND COALESCE(o.nivel1_email,'') = ''
          AND COALESCE(ed.dominio,'') <> ''
        ORDER BY o.empresa
    """)
    targets = cur.fetchall()
    if args.limit: targets = targets[:args.limit]
    print(f"\nPDF harvest: {len(targets)} (empresa,dominio) targets\n")

    if not args.commit:
        for e,d in targets[:10]: print(f"  {e[:40]:40} @ {d}")
        return

    total_pdfs = total_emails = total_phones = obras_email = obras_tel = 0
    t0 = time.time()
    for i,(empresa, dominio) in enumerate(targets,1):
        emails_all, phones_all = set(), set()
        for q in [f'"@{dominio}" filetype:pdf (fornecedor OR compras OR contato)',
                  f'"{empresa}" filetype:pdf (codigo OR conduta OR compras)']:
            for r in serper(q, 3):
                url = r.get("link") or ""
                if not url.lower().endswith(".pdf"): continue
                pdf_bytes = fetch_pdf(url)
                if not pdf_bytes: continue
                total_pdfs += 1
                texto = extract_pdf_text(pdf_bytes)
                for em in EMAIL_RE.findall(texto):
                    em_low = em.lower()
                    if em_low.endswith("@"+dominio.lower()) and not any(em_low.startswith(b) for b in EMAIL_BL):
                        emails_all.add(em_low)
                phones_all.update(parse_phones(texto))
        total_emails += len(emails_all); total_phones += len(phones_all)
        print(f"[{i:02d}/{len(targets)}] {empresa[:32]:32} dom={dominio[:20]:20} pdfs OK | emails={len(emails_all)} phones={len(phones_all)}")

        # Cross-match com decisores OR fallback genérico
        cur.execute("""
            SELECT id::text, nivel1_nome FROM obras
            WHERE classificacao_computed IN ('OURO','PRATA') AND visivel AND empresa = %s
              AND COALESCE(nivel1_email,'')=''
        """, (empresa,))
        obras_target = cur.fetchall()
        for oid, nome in obras_target:
            best_em = None
            if nome and len(nome.split()) >= 2:
                parts = [p.lower() for p in re.split(r"\s+", nome) if len(p)>2]
                for em in emails_all:
                    local = em.split("@")[0].lower()
                    if parts[0] in local and parts[-1] in local: best_em = em; break
                    if parts[-1] in local and not best_em: best_em = em
            if not best_em:
                for kp in GENERIC_B2B:
                    cand = [e for e in emails_all if e.startswith(kp)]
                    if cand: best_em = cand[0]; break
            if best_em:
                cur.execute("UPDATE obras SET nivel1_email=%s WHERE id=%s AND COALESCE(nivel1_email,'')=''", (best_em, oid))
                if cur.rowcount: obras_email += 1
            # Phone (qualquer válido)
            if phones_all:
                phone_e164 = next(iter(phones_all))
                try:
                    p = phonenumbers.parse(phone_e164, None)
                    nat = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL)
                except Exception: nat = phone_e164
                cur.execute("""
                    UPDATE obras SET nivel1_telefone=%s, nivel1_telefone_e164=%s, nivel1_telefone_status='valido'
                    WHERE id=%s AND COALESCE(nivel1_telefone,'')=''
                """, (nat, phone_e164, oid))
                if cur.rowcount: obras_tel += 1
        conn.commit()

    print(f"\n  PDFs processados: {total_pdfs}")
    print(f"  Emails (bruto): {total_emails} | Phones (bruto): {total_phones}")
    print(f"  Obras com email novo: {obras_email}")
    print(f"  Obras com phone novo: {obras_tel}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
