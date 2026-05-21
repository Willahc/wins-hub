"""
scrape_spa_playwright.py — Playwright headless + Haiku pra SPAs corporativas.

Renderiza JS (necessário pra Aegea/Vale/Petrobras modernos), passa HTML pra Haiku
extrair emails @dominio + phones BR. UPDATE obras.nivel1_*.
"""
import argparse, asyncio, json, os, re, sys, time
sys.path.insert(0, "/app")
import anthropic
import phonenumbers
import psycopg2
from playwright.async_api import async_playwright

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}
MODEL = "claude-haiku-4-5-20251001"

EMAIL_BL = ("noreply","no-reply","dpo","lgpd","ouvidoria","imprensa","rh@","trainee","vagas")
GENERIC_B2B = ("fornecedores@","compras@","suprimentos@","comercial@","contato@","atendimento@","sac@")

SYSTEM = """Auditor de páginas corporativas brasileiras renderizadas (SPA).
Extraia APENAS:
- Emails @{dominio} de contato/compras/suprimentos/comercial visíveis no texto
- Telefones BR (DDD + 8/9 dígitos) de contato/atendimento (não inclua timestamps, IDs)

Descarte: noreply, dpo, vagas, trainee, números de versão/timestamps/JS.

JSON apenas: {"emails":[{"email":"x@y","setor":"compras|geral|outro"}],"telefones":[{"numero":"(21)1234-5678","setor":"geral"}]}"""


async def fetch_rendered(url, timeout=30000):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent="Mozilla/5.0 (compatible; WinsHubBot/1.0)")
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)  # aguarda hydration
            # Tenta navegar pra "contato" se houver link
            try:
                await page.click('a:has-text("Contato"), a:has-text("Fale Conosco"), a:has-text("Fornecedor")', timeout=3000)
                await page.wait_for_timeout(2000)
            except Exception:
                pass
            text_content = await page.evaluate("() => document.body.innerText")
            return text_content[:30000]
        except Exception as e:
            return None
        finally:
            await browser.close()


_client = anthropic.Anthropic()

def haiku_extract(texto, dominio):
    user = f"Domínio principal: {dominio}\n\nTexto da página renderizada:\n{texto[:25000]}"
    try:
        resp = _client.messages.create(
            model=MODEL, max_tokens=600,
            system=SYSTEM.replace("{dominio}", dominio),
            messages=[{"role":"user","content":user}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*","",raw); raw = re.sub(r"\s*```$","",raw)
        return json.loads(raw)
    except Exception as e:
        return {"emails":[], "telefones":[], "erro":str(e)[:100]}


async def main():
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
          AND o.empresa NOT ILIKE 'ESTADO%' AND o.empresa NOT ILIKE 'SECRETARIA%'
          AND o.empresa NOT ILIKE 'PREFEITURA%' AND o.empresa NOT ILIKE 'MUNICIPIO%'
        ORDER BY o.empresa
    """)
    targets = cur.fetchall()
    if args.limit: targets = targets[:args.limit]
    print(f"\nSPA scrape: {len(targets)} (empresa,dominio) targets\n")
    if not args.commit:
        for e,d in targets[:10]: print(f"  {e[:40]:40} @ {d}")
        return

    obras_em = obras_tel = 0
    t0 = time.time()
    for i,(empresa,dominio) in enumerate(targets,1):
        url = f"https://www.{dominio}"
        texto = await fetch_rendered(url)
        if not texto:
            print(f"[{i:02d}/{len(targets)}] {empresa[:30]:30} dom={dominio[:20]} - fetch_fail"); continue
        extracted = haiku_extract(texto, dominio)
        emails_validos = []
        for em in extracted.get("emails",[]):
            e_low = (em.get("email") or "").lower()
            if e_low.endswith("@"+dominio.lower()) and not any(b in e_low for b in EMAIL_BL):
                emails_validos.append(e_low)
        phones_validos = []
        for tel in extracted.get("telefones",[]):
            num = (tel.get("numero") or "").strip()
            try:
                p = phonenumbers.parse(num,"BR")
                if phonenumbers.is_valid_number(p):
                    e164 = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
                    nat = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL)
                    phones_validos.append({"e164":e164,"nat":nat})
            except phonenumbers.NumberParseException: continue

        print(f"[{i:02d}/{len(targets)}] {empresa[:30]:30} dom={dominio[:20]:20} emails={len(emails_validos)} phones={len(phones_validos)}")
        if not emails_validos and not phones_validos: continue

        # Best email genérico B2B
        best_em = next((e for kp in GENERIC_B2B for e in emails_validos if e.startswith(kp)), None)
        if not best_em and emails_validos: best_em = emails_validos[0]
        best_tel = phones_validos[0] if phones_validos else None

        cur.execute("""
            SELECT id::text FROM obras WHERE classificacao_computed IN ('OURO','PRATA') AND visivel AND empresa=%s
              AND (COALESCE(nivel1_email,'')='' OR COALESCE(nivel1_telefone,'')='')
        """,(empresa,))
        for (oid,) in cur.fetchall():
            sets, params = [], []
            if best_em:
                sets.append("nivel1_email=COALESCE(NULLIF(nivel1_email,''),%s)"); params.append(best_em)
            if best_tel:
                sets.append("nivel1_telefone=COALESCE(NULLIF(nivel1_telefone,''),%s)")
                sets.append("nivel1_telefone_e164=COALESCE(NULLIF(nivel1_telefone_e164,''),%s)")
                sets.append("nivel1_telefone_status=COALESCE(NULLIF(nivel1_telefone_status,''),'valido')")
                params += [best_tel["nat"], best_tel["e164"]]
            if sets:
                params.append(oid)
                cur.execute(f"UPDATE obras SET {', '.join(sets)} WHERE id=%s", params)
                if cur.rowcount:
                    if best_em: obras_em += 1
                    if best_tel: obras_tel += 1
        conn.commit()

    print(f"\n  Obras email novo: {obras_em}")
    print(f"  Obras phone novo: {obras_tel}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    asyncio.run(main())
