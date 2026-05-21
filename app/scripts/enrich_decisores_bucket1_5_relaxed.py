"""
enrich_decisores_bucket1_5_relaxed.py — Variante relaxada do bucket 1.5.

Aceita decisor mesmo SEM tipo_cargo extraído, desde que:
- Nome >=2 palavras, cada >=3 chars
- LinkedIn slug válido
- decisor_gate aprove (Gate 2 decisor_unico_cluster cobre)

Usar pra empresas onde descobrir_via_search_engines achou candidatos mas extração
de cargo falhou (Dechra/EDP/Mahindra/farmacêuticas/estrangeiras menores).
"""
import argparse, os, re, sys, time
sys.path.insert(0, "/app")
import psycopg2
import psycopg2.extras
from unidecode import unidecode

from sales_intelligence.camada3_decisores.linkedin_search import descobrir_via_search_engines
from sales_intelligence.decisor_gate import decisor_inserivel

DB = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}

CANDIDATOS_SQL = """
WITH metas AS (
  SELECT o.id, o.empresa, o.cnpj, o.classificacao_computed AS tier,
    (SELECT COUNT(*) FROM decisores_obra d
       WHERE d.obra_id=o.id AND d.excluido_em IS NULL
         AND d.tipo_cargo IS NOT NULL AND d.tipo_cargo <> '' AND d.tipo_cargo <> 'OUTRO'
         AND COALESCE(d.hipotese_replicacao,'') <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO'
         AND d.nome NOT ILIKE 'Contato Comercial%' AND d.nome NOT ILIKE 'Equipe %'
         AND d.nome NOT ILIKE 'Gerência %' AND d.nome NOT ILIKE 'Site Manager%') AS qtd_dec_real
  FROM obras o
  WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel
    AND COALESCE(o.empresa,'') <> ''
    AND o.empresa NOT ILIKE 'ESTADO%' AND o.empresa NOT ILIKE 'SECRETARIA%' AND o.empresa NOT ILIKE 'PREFEITURA%' AND o.empresa NOT ILIKE 'MUNICIPIO%' AND o.empresa NOT ILIKE 'GOVERNO%'
)
SELECT id::text AS obra_id, empresa, COALESCE(cnpj,'') AS cnpj, tier
FROM metas
WHERE qtd_dec_real = 0
ORDER BY empresa, tier
"""

def _nome_qualidade(nome):
    if not nome: return False
    parts = [p for p in re.split(r"\s+", nome) if p]
    if len(parts) < 2: return False
    palavras_alfa = [p for p in parts if len(p) >= 3 and any(c.isalpha() for c in p)]
    return len(palavras_alfa) >= 2

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(CANDIDATOS_SQL)
    obras = cur.fetchall()

    por_empresa = {}
    for o in obras:
        emp_key = unidecode((o["empresa"] or "").lower().strip())
        por_empresa.setdefault(emp_key, []).append(dict(o))
    empresas = list(por_empresa.keys())
    if args.limit: empresas = empresas[:args.limit]

    print(f"\nBucket 1.5 RELAXED: {len(obras)} obras alvo, {len(empresas)} empresas")
    print(f"Modo: {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    inseridos = 0; gate_rej = 0; skip_existe = 0
    t0 = time.time()
    for i, emp_key in enumerate(empresas, 1):
        obras_emp = por_empresa[emp_key]
        empresa_nome = obras_emp[0]["empresa"]
        cnpj = obras_emp[0]["cnpj"] or None
        print(f"\n[{i:02d}/{len(empresas)}] {empresa_nome[:55]} ({len(obras_emp)} obras)")
        try:
            decisores = descobrir_via_search_engines(empresa_nome, cnpj=cnpj, max_buckets=11)
        except Exception as e:
            print(f"  ERR: {e!r}"); continue
        if not decisores: continue
        # Ranking: nome qualidade + LK slug + score implícito
        candidatos = [d for d in decisores
                      if d.nome_pessoa and _nome_qualidade(d.nome_pessoa)
                      and d.linkedin_slug and len(d.linkedin_slug) >= 5]
        print(f"  Total bruto={len(decisores)} qualificados={len(candidatos)}")

        for obra in obras_emp:
            inseridos_essa = 0
            for dec in candidatos:
                if inseridos_essa >= 2: break
                cur.execute("SELECT 1 FROM decisores_obra WHERE obra_id=%s AND lower(nome)=lower(%s) AND excluido_em IS NULL LIMIT 1",
                            (obra["obra_id"], dec.nome_pessoa))
                if cur.fetchone(): skip_existe += 1; continue
                permite, motivo = decisor_inserivel(cur, dec.nome_pessoa, dec.cargo_raw or "", empresa_nome)
                if not permite: gate_rej += 1; continue
                if args.commit:
                    linkedin_url = f"https://br.linkedin.com/in/{dec.linkedin_slug}" if dec.linkedin_slug else None
                    cur.execute("""
                        INSERT INTO decisores_obra (obra_id, nome, cargo, tipo_cargo, linkedin_url, fonte, registrado_por)
                        VALUES (%s,%s,%s,%s,%s,'tecnica_mari_relaxed','bucket1_5_relaxed')
                        ON CONFLICT DO NOTHING
                    """, (obra["obra_id"], dec.nome_pessoa, dec.cargo_raw or '', dec.tipo_cargo, linkedin_url))
                    if cur.rowcount:
                        inseridos += 1; inseridos_essa += 1
                        print(f"  + {dec.nome_pessoa[:30]:30} | tipo={dec.tipo_cargo or 'None':18} | gate={motivo}")
                else:
                    inseridos += 1; inseridos_essa += 1
                    print(f"  ?? {dec.nome_pessoa[:30]:30} | {(dec.cargo_raw or '')[:35]} | tipo={dec.tipo_cargo}")
        if args.commit: conn.commit()

    print(f"\n  Inseridos: {inseridos}  | Gate rejeitou: {gate_rej}  | Skip duplicado: {skip_existe}")
    print(f"  Tempo: {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
