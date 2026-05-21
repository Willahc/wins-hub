"""
enrich_phone_decisor_via_serper.py — Busca telefone DIRETO do decisor via Serper.

Estratégia: query 'nome empresa telefone/contato' → parse title+snippet com regex BR
phones → validar via phonenumbers → UPDATE decisores_obra.telefone.

Pequeno hit rate esperado (C-level às vezes tem phone em press releases) mas zero custo.
"""
import argparse, os, re, sys, time
sys.path.insert(0, "/app")
import phonenumbers
import psycopg2
import requests

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}
SERPER = "https://google.serper.dev/search"

# Regex phones BR comuns: (DD)NNNN-NNNN, DD NNNNN-NNNN, +55 DD..., 0800 etc
PHONE_RE = re.compile(
    r"(?:\+?55[\s.-]?)?\(?0?(\d{2})\)?[\s.-]?(\d{4,5})[\s.-]?(\d{4})"
)

def serper(query, num=5):
    key = os.getenv("SERPER_API_KEY","").strip()
    if not key: return []
    try:
        r = requests.post(SERPER, json={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type":"application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code==200 else []
    except requests.RequestException: return []


def buscar_phone(nome, empresa):
    queries = [
        f'"{nome}" "{empresa}" (telefone OR celular OR contato OR fone)',
        f'"{nome}" "{empresa}" -inurl:linkedin.com',
    ]
    candidatos = set()
    for q in queries:
        for r in serper(q, 5):
            blob = f"{r.get('title','')} {r.get('snippet','')}"
            for ddd, p1, p2 in PHONE_RE.findall(blob):
                num = f"{ddd}{p1}{p2}"
                try:
                    p = phonenumbers.parse(num, "BR")
                    if phonenumbers.is_valid_number(p):
                        e164 = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
                        # Skip 0800 etc
                        if not e164.startswith("+550800"):
                            candidatos.add(e164)
                except phonenumbers.NumberParseException:
                    continue
    # Retorna o mais comum (heurística: phones reais aparecem múltiplas vezes; só 1 candidato = baixa confiança)
    if not candidatos: return None
    # Se múltiplos, pega o que tem DDD da empresa (futuro melhoramento). Por ora: primeiro válido.
    return next(iter(candidatos))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-tier", default="OURO,PRATA", help="OURO[,PRATA]")
    args = ap.parse_args()

    tiers = tuple(t.strip() for t in args.only_tier.split(","))
    conn = psycopg2.connect(**DB); cur = conn.cursor()
    cur.execute(r"""
        SELECT DISTINCT ON (d.nome)
          d.id::text, d.nome, d.cargo,
          o.empresa, o.classificacao_computed AS tier
        FROM decisores_obra d
        JOIN obras o ON o.id=d.obra_id
        WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
          AND d.excluido_em IS NULL
          AND COALESCE(d.telefone,'') = ''
          AND COALESCE(d.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
          AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> '' AND d.tipo_cargo <> 'OUTRO'
          AND d.nome NOT ILIKE 'Contato Comercial%' AND d.nome NOT ILIKE 'Equipe %'
          AND d.nome NOT ILIKE 'Gerência %' AND d.nome NOT ILIKE 'Site Manager%'
          AND d.nome NOT ILIKE 'Project Manager%' AND d.nome NOT ILIKE 'Dir. %'
          AND d.nome ~ '[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}'
          AND COALESCE(o.empresa,'') <> ''
        ORDER BY d.nome
    """)
    candidatos = cur.fetchall()
    if args.limit: candidatos = candidatos[:args.limit]

    print(f"\nPhone direto decisor: {len(candidatos)} decisores únicos ({'COMMIT' if args.commit else 'DRY-RUN'})\n")
    if not args.commit:
        for c in candidatos[:10]: print(f"  {c[1][:30]:30} @ {c[3][:40]}")
        return

    n_found = 0
    t0 = time.time()
    for i, (dec_id, nome, cargo, empresa, tier) in enumerate(candidatos, 1):
        phone = buscar_phone(nome, empresa)
        if phone:
            n_found += 1
            cur.execute(r"""
                UPDATE decisores_obra SET telefone=%s,
                  confianca_match_componentes = COALESCE(confianca_match_componentes,'{}'::jsonb)
                    || jsonb_build_object('phone_via_serper', jsonb_build_object('ts',NOW()::text))
                WHERE lower(nome)=lower(%s) AND COALESCE(telefone,'')='' AND excluido_em IS NULL
            """, (phone, nome))
            n_obras = cur.rowcount
            print(f"[{i:03d}/{len(candidatos)}] OK {nome[:30]:30} {tier:5} → {phone} ({n_obras} obras)")
        else:
            print(f"[{i:03d}/{len(candidatos)}] -- {nome[:30]:30} @ {empresa[:30]}")
        conn.commit()
        time.sleep(0.4)

    print(f"\n  Phones encontrados: {n_found}/{len(candidatos)}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
