"""
enrich_decisores_estatais.py — Descobrir Secretário/Diretor estatal via Serper.

Pra obras OURO/PRATA de entidades públicas (Estado/Secretaria/Prefeitura),
classifica órgão pelo nome da obra + Serper search pelo decisor responsável.
"""
import argparse, os, re, sys, time
sys.path.insert(0, "/app")
import psycopg2
import requests
from unidecode import unidecode

from sales_intelligence.decisor_gate import decisor_inserivel

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}
SERPER = "https://google.serper.dev/search"

# Mapeamento tipo de obra → órgão/cargo provável
TIPO_ORGAO = [
    (r"rodovi[ao]|pavimenta|duplica|asfalt", "DER", "Diretor Geral DER OR Secretário de Infraestrutura"),
    (r"monotrilho|metrô|linha\s+\d|mobilidade urbana", "STM", "Secretário de Transportes Metropolitanos OR Diretor STM"),
    (r"recursos h[ií]dricos|abastecimento|saneamento|esgoto|[aá]gua", "CAGECE", "Secretário de Recursos Hídricos OR Presidente Companhia Saneamento"),
    (r"sa[uú]de|hospital|UPA|UBS", "SAUDE", "Secretário de Saúde"),
    (r"educa[cç][ãa]o|escola|universidade", "EDUCACAO", "Secretário de Educação"),
    (r"agricultura|agrícola|agropecu[áa]ria", "AGRICULTURA", "Secretário de Agricultura"),
    (r"mobilidade|transporte", "TRANSPORTES", "Secretário de Transportes OR Mobilidade"),
    (r"infraestrutura|infra-?estrutura|investimento.+mult", "INFRA", "Secretário de Infraestrutura OR Planejamento"),
    (r"desenvolvimento urbano|urbano|reforma", "DESENV_URB", "Secretário de Desenvolvimento Urbano"),
    (r"meio ambiente|ambiental", "MEIO_AMB", "Secretário de Meio Ambiente"),
    (r"obra|engenharia", "OBRAS", "Secretário de Obras"),
    (r"", "GENERICO", "Secretário de Infraestrutura OR Obras OR Planejamento"),
]

NOME_RE = re.compile(
    r"\b([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}(?:\s+(?:da|de|do|dos|das)?\s*[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}){1,4})\b"
)
NAMES_BLACKLIST = {"Governo", "Secretaria", "Estado", "Diretor", "Secretário",
                   "Ministério", "Ministerio", "Presidente", "Brasília", "Brasilia",
                   "São Paulo", "Sao Paulo", "Rio De Janeiro", "Brasil", "Janeiro",
                   "Diário Oficial", "Diario Oficial", "Tribunal", "Justiça"}


def serper(query, num=5):
    key = os.getenv("SERPER_API_KEY","").strip()
    if not key: return []
    try:
        r = requests.post(SERPER, json={"q": query, "num": num},
            headers={"X-API-KEY": key, "Content-Type":"application/json"}, timeout=15)
        return (r.json().get("organic") or []) if r.status_code==200 else []
    except requests.RequestException: return []


def classificar(nome_obra):
    txt = unidecode((nome_obra or "").lower())
    for pat, tag, query in TIPO_ORGAO:
        if pat and re.search(pat, txt):
            return tag, query
    return "GENERICO", TIPO_ORGAO[-1][2]


def buscar_decisor(empresa, uf, query_termos):
    estado_clean = empresa.replace("ESTADO DO", "").replace("ESTADO DE", "").replace("ESTADO DA", "").strip()
    if not estado_clean: estado_clean = uf
    queries = [
        f'({query_termos}) "{estado_clean}" {uf}',
        f'"Secretário" "{estado_clean}" obras OR infraestrutura',
    ]
    candidatos = {}
    for q in queries:
        for r in serper(q, 8):
            blob = f"{r.get('title','')} {r.get('snippet','')}"
            for m in NOME_RE.finditer(blob):
                nome = m.group(1).strip()
                parts = nome.split()
                if len(parts) < 2 or len(parts) > 4: continue
                if any(b in nome for b in NAMES_BLACKLIST): continue
                if parts[0] in NAMES_BLACKLIST: continue
                # Buscar contexto cargo near nome
                i = m.start()
                contexto = blob[max(0,i-150):i+150].lower()
                cargo_kw = ["secretár", "diretor", "presidente", "superintendent", "coordenador"]
                if any(kw in contexto for kw in cargo_kw):
                    candidatos[nome.lower()] = nome
    return list(candidatos.values())[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB); cur = conn.cursor()
    cur.execute("""
        SELECT id::text, empresa, COALESCE(uf,'') AS uf, COALESCE(nome,'') AS nome_obra,
               classificacao_computed AS tier
        FROM obras
        WHERE classificacao_computed IN ('OURO','PRATA') AND visivel
          AND (empresa ILIKE 'ESTADO%' OR empresa ILIKE 'SECRETARIA%' OR empresa ILIKE 'PREFEITURA%'
               OR empresa ILIKE 'MUNICIPIO%' OR empresa ILIKE 'GOVERNO%')
          AND COALESCE(nivel1_nome,'') = ''
        ORDER BY valor_estimado DESC NULLS LAST
    """)
    obras = cur.fetchall()
    if args.limit: obras = obras[:args.limit]
    print(f"\nEstatais: {len(obras)} obras ({'COMMIT' if args.commit else 'DRY-RUN'})\n")
    if not args.commit:
        for o in obras[:5]: print(f"  {o[1][:40]} {o[2]} → {o[3][:50]}")
        return

    inseridos = 0; gate_rej = 0
    t0 = time.time()
    for i, (obra_id, empresa, uf, nome_obra, tier) in enumerate(obras, 1):
        tag, query_termos = classificar(nome_obra)
        nomes = buscar_decisor(empresa, uf, query_termos)
        if not nomes:
            print(f"[{i:02d}/{len(obras)}] -- {empresa[:30]:30} {uf} | {tag:10}")
            continue
        for nome in nomes[:1]:  # top 1 candidato
            cargo = f"Secretário ({tag})"
            permite, motivo = decisor_inserivel(cur, nome, cargo, empresa)
            if not permite: gate_rej += 1; continue
            cur.execute("""
                INSERT INTO decisores_obra (obra_id, nome, cargo, tipo_cargo, fonte, registrado_por)
                VALUES (%s,%s,%s,'OUTRO','serper_estatais_dou','enrich_decisores_estatais')
                ON CONFLICT DO NOTHING
            """, (obra_id, nome, cargo))
            if cur.rowcount:
                inseridos += 1
                print(f"[{i:02d}/{len(obras)}] OK {empresa[:30]:30} {uf} | {tag:10} → {nome}")
                break
        conn.commit()

    print(f"\n  Inseridos: {inseridos}  Gate rej: {gate_rej}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
