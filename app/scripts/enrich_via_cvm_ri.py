"""
enrich_via_cvm_ri.py — Descobre Diretor de Relações com Investidores (DRI) via páginas RI.

Companhias listadas B3 são obrigadas a publicar DRI com email/telefone na página RI
(ri.empresa.com.br ou similar). Estratégia:
  1. Pra cada empresa OURO/PRATA, query Serper `"empresa" "DRI" OR "Diretor de Relações" email`
  2. Filter URLs de RI (ri., investidor, ir., relacoes)
  3. Fetch via requests + Haiku extract: nome DRI + email + phone
  4. INSERT via decisor_gate
"""
import argparse, json, os, re, sys, time
sys.path.insert(0, "/app")
import anthropic
import phonenumbers
import psycopg2
import requests
from unidecode import unidecode
from sales_intelligence.decisor_gate import decisor_inserivel

DB = {"host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
      "dbname": os.getenv("DB_NAME","wins_hub"), "user": os.getenv("DB_USER","postgres"),
      "password": os.getenv("DB_PASSWORD","")}
SERPER = "https://google.serper.dev/search"
MODEL = "claude-haiku-4-5-20251001"

SYSTEM = """Auditor de páginas Relações com Investidores (RI) brasileiras.
Extrai APENAS:
- Nome do Diretor de Relações com Investidores (DRI) ou equivalente
- Email DRI (no domínio @{dominio})
- Telefone DRI (formato BR)

JSON: {"dri_nome":"...","dri_cargo":"...","email":"x@y","telefone":"(21)1234-5678"} ou {} se não encontrar.
Sem markdown."""

def serper(q, num=5):
    key = os.getenv("SERPER_API_KEY","").strip()
    if not key: return []
    try:
        r = requests.post(SERPER, json={"q": q, "num": num},
            headers={"X-API-KEY": key, "Content-Type":"application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code==200 else []
    except: return []

def fetch(url, max_bytes=80000):
    try:
        r = requests.get(url, timeout=20,
            headers={"User-Agent":"Mozilla/5.0 (compatible; WinsHubBot/1.0)"},
            allow_redirects=True)
        if r.status_code != 200: return None
        text = r.text[:max_bytes]
        text = re.sub(r"<script[^>]*>.*?</script>"," ", text, flags=re.DOTALL|re.I)
        text = re.sub(r"<style[^>]*>.*?</style>"," ", text, flags=re.DOTALL|re.I)
        text = re.sub(r"<[^>]+>"," ", text)
        return re.sub(r"\s+"," ", text).strip()[:30000]
    except: return None

_client = anthropic.Anthropic()
def haiku(texto, dominio):
    try:
        r = _client.messages.create(model=MODEL, max_tokens=300,
            system=SYSTEM.replace("{dominio}", dominio),
            messages=[{"role":"user","content":texto[:25000]}])
        raw = r.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*","",raw); raw = re.sub(r"\s*```$","",raw)
        return json.loads(raw)
    except: return {}

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
          AND o.empresa NOT ILIKE 'ESTADO%' AND o.empresa NOT ILIKE 'SECRETARIA%'
          AND o.empresa NOT ILIKE 'PREFEITURA%' AND o.empresa NOT ILIKE 'MUNICIPIO%'
        ORDER BY o.empresa
    """)
    targets = cur.fetchall()
    if args.limit: targets = targets[:args.limit]
    print(f"\nCVM/RI: {len(targets)} (empresa,dominio) targets\n")
    if not args.commit:
        for e,d in targets[:10]: print(f"  {e[:40]:40} @ {d}")
        return

    inseridos = 0; pegou_em = 0; pegou_tel = 0
    t0 = time.time()
    for i,(empresa, dominio) in enumerate(targets,1):
        # Query Serper RI
        results = serper(f'"{empresa}" "Diretor de Relações com Investidores" OR "DRI" email', 8)
        ri_urls = [r.get("link","") for r in results
                   if any(k in (r.get("link","")).lower() for k in ("ri.","relacoes","investidor","/ir/","mzi","mziq"))]
        ri_urls = ri_urls[:3]
        dri_info = {}
        for url in ri_urls:
            texto = fetch(url)
            if not texto: continue
            res = haiku(texto, dominio)
            if res.get("dri_nome"):
                dri_info = res
                break

        if not dri_info.get("dri_nome"):
            print(f"[{i:02d}/{len(targets)}] -- {empresa[:35]:35}")
            continue

        nome = dri_info["dri_nome"]
        cargo = dri_info.get("dri_cargo") or "Diretor de Relações com Investidores"
        email = (dri_info.get("email") or "").lower()
        if email and not email.endswith("@" + dominio.lower()):
            email = None
        tel_e164 = None; tel_nat = None
        if dri_info.get("telefone"):
            try:
                p = phonenumbers.parse(dri_info["telefone"], "BR")
                if phonenumbers.is_valid_number(p):
                    tel_e164 = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
                    tel_nat = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL)
            except: pass

        # INSERT em todas obras dessa empresa
        cur.execute("""
            SELECT id::text FROM obras WHERE classificacao_computed IN ('OURO','PRATA') AND visivel AND empresa = %s
        """, (empresa,))
        n = 0
        for (oid,) in cur.fetchall():
            permite, motivo = decisor_inserivel(cur, nome, cargo, empresa)
            if not permite: continue
            cur.execute("""
                INSERT INTO decisores_obra (obra_id, nome, cargo, tipo_cargo, email, telefone, fonte, registrado_por)
                VALUES (%s,%s,%s,'OUTRO',%s,%s,'cvm_ri','enrich_via_cvm_ri')
                ON CONFLICT DO NOTHING
            """, (oid, nome, cargo, email, tel_e164))
            if cur.rowcount:
                inseridos += 1; n += 1
                if email: pegou_em += 1
                if tel_e164: pegou_tel += 1
                # Sync obras.nivel1 se vazio
                sets, params = [], []
                sets.append("nivel1_nome = COALESCE(NULLIF(nivel1_nome,''), %s)"); params.append(nome)
                sets.append("nivel1_cargo = COALESCE(NULLIF(nivel1_cargo,''), %s)"); params.append(cargo)
                if email:
                    sets.append("nivel1_email = COALESCE(NULLIF(nivel1_email,''), %s)"); params.append(email)
                if tel_nat:
                    sets.append("nivel1_telefone = COALESCE(NULLIF(nivel1_telefone,''), %s)"); params.append(tel_nat)
                    sets.append("nivel1_telefone_e164 = COALESCE(NULLIF(nivel1_telefone_e164,''), %s)"); params.append(tel_e164)
                    sets.append("nivel1_telefone_status = COALESCE(NULLIF(nivel1_telefone_status,''), 'valido')")
                params.append(oid)
                cur.execute(f"UPDATE obras SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()
        print(f"[{i:02d}/{len(targets)}] OK {empresa[:35]:35} DRI={nome[:25]} em={'✓' if email else '-'} tel={'✓' if tel_e164 else '-'} ({n} obras)")

    print(f"\n  Inseridos: {inseridos}  emails: {pegou_em}  phones: {pegou_tel}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
