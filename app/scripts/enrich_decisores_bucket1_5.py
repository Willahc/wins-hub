"""
enrich_decisores_bucket1_5.py — Descobre decisor real de obras OURO/PRATA via
tecnica Mari (descobrir_via_search_engines + 106 cargos PT/EN).

Fluxo:
  1. Lista obras OURO/PRATA visíveis sem decisor OU só com placeholder
  2. Pra cada: descobrir_via_search_engines(empresa, max_buckets=11)
     - 106 cargos PT/EN inclui Capex, Supply Chain, Procurement, etc
  3. Filtra com decisor_gate (cargo cita empresa OU decisor único)
  4. INSERT em decisores_obra (fonte='tecnica_mari_bucket1.5')

Dry-run --commit obrigatório pra escrever.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras
from unidecode import unidecode

from sales_intelligence.camada3_decisores.linkedin_search import descobrir_via_search_engines
from sales_intelligence.decisor_gate import decisor_inserivel


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

CANDIDATOS_SQL = """
WITH metas AS (
  SELECT o.id, o.empresa, o.cnpj, o.classificacao_computed AS tier,
    (SELECT COUNT(*) FROM decisores_obra d
       WHERE d.obra_id=o.id AND d.excluido_em IS NULL) AS qtd_dec,
    (SELECT COUNT(*) FROM decisores_obra d
       WHERE d.obra_id=o.id AND d.excluido_em IS NULL
         AND (d.nome ILIKE 'Contato Comercial%' OR d.nome ILIKE 'Equipe %'
              OR d.nome ILIKE 'Equip %' OR d.nome ILIKE 'Gerência %'
              OR d.nome ILIKE 'Gerencia %' OR d.nome ILIKE 'Departamento%'
              OR d.nome ILIKE 'Setor%' OR d.nome ILIKE 'Diretoria%'
              OR d.nome ILIKE 'Direção%' OR d.nome ILIKE 'Direcao%'
              OR d.nome ILIKE 'Coordenação%' OR d.nome ILIKE 'Coordenacao%'
              OR d.nome ILIKE 'Time %' OR d.nome ILIKE 'Site Manager%'
              OR d.nome ILIKE 'Project Manager%')) AS qtd_placeholder
  FROM obras o
  WHERE o.classificacao_computed IN ('OURO','PRATA') AND o.visivel = true
    AND COALESCE(o.empresa,'') <> ''
)
SELECT id::text AS obra_id, empresa, COALESCE(cnpj,'') AS cnpj, tier
FROM metas
WHERE qtd_dec = 0 OR (qtd_dec > 0 AND qtd_placeholder = qtd_dec)
ORDER BY empresa, tier
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-buckets", type=int, default=11,
                    help="quantos buckets de cargos (11 cobre todos 106 termos)")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(CANDIDATOS_SQL)
    obras = cur.fetchall()

    # Dedup por empresa — uma chamada Serper por empresa, aplica em todas as obras
    por_empresa: dict[str, list] = {}
    for o in obras:
        emp_key = unidecode((o["empresa"] or "").lower().strip())
        por_empresa.setdefault(emp_key, []).append(dict(o))
    empresas = list(por_empresa.keys())
    if args.limit:
        empresas = empresas[: args.limit]

    print(f"\nBucket 1.5 — descobrir_via_search_engines (max_buckets={args.max_buckets})")
    print(f"  Obras alvo: {len(obras)}")
    print(f"  Empresas únicas: {len(por_empresa)} → {len(empresas)} processadas")
    print(f"  Modo: {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    total_descobertos = 0
    total_inseridos = 0
    total_gate_reject = 0
    total_skip_existe = 0
    t0 = time.time()

    for i, emp_key in enumerate(empresas, 1):
        obras_emp = por_empresa[emp_key]
        empresa_nome = obras_emp[0]["empresa"]
        cnpj = obras_emp[0]["cnpj"] or None
        print(f"\n[{i:02d}/{len(empresas)}] {empresa_nome[:55]} ({len(obras_emp)} obras)")
        try:
            decisores = descobrir_via_search_engines(empresa_nome, cnpj=cnpj,
                                                      max_buckets=args.max_buckets)
        except Exception as e:
            print(f"  ERR descobrir: {e!r}")
            continue
        print(f"  Encontrados: {len(decisores)} candidatos")
        total_descobertos += len(decisores)
        if not decisores:
            continue

        # Priorizar decisores reais de compras/supply/engenharia (não OUTRO genérico)
        PRIORIDADE_TIPO = [
            "SUPPLY_CHAIN", "GERENTE_SUPRIMENTOS", "GERENTE_COMPRAS",
            "GERENTE_PROJETOS", "GERENTE_INDUSTRIAL", "COORDENADOR_OBRAS",
            "COORDENADOR_MANUTENCAO", "GERENTE_ENGENHARIA",
            "ENGENHEIRO_MECANICO_CIVIL", "PROJETISTA",
        ]
        def _ordem(d):
            try:
                return PRIORIDADE_TIPO.index(d.tipo_cargo or "")
            except ValueError:
                return 99
        decisores_ordenados = sorted(decisores, key=_ordem)

        # Pra cada obra dessa empresa, tentar inserir top-2 decisores prioritários
        for obra in obras_emp:
            inseridos_essa_obra = 0
            for dec in decisores_ordenados:
                if inseridos_essa_obra >= 2:
                    break
                if not dec.nome_pessoa or not dec.cargo_raw:
                    continue
                # Pula tipo_cargo None/OUTRO se ainda não inserimos 1 prioritário
                if dec.tipo_cargo not in PRIORIDADE_TIPO and inseridos_essa_obra == 0:
                    # primeiro insert tem que ser prioritário; se nenhum encontrar, ok 0 obras
                    continue
                cur.execute(
                    """SELECT 1 FROM decisores_obra
                       WHERE obra_id=%s AND lower(nome)=lower(%s) AND excluido_em IS NULL LIMIT 1""",
                    (obra["obra_id"], dec.nome_pessoa),
                )
                if cur.fetchone():
                    total_skip_existe += 1
                    continue
                permite, motivo = decisor_inserivel(
                    cur, dec.nome_pessoa, dec.cargo_raw, empresa_nome
                )
                if not permite:
                    total_gate_reject += 1
                    continue
                if args.commit:
                    linkedin_url = (
                        f"https://br.linkedin.com/in/{dec.linkedin_slug}"
                        if dec.linkedin_slug else None
                    )
                    cur.execute(
                        """
                        INSERT INTO decisores_obra
                          (obra_id, nome, cargo, tipo_cargo, linkedin_url, fonte, registrado_por)
                        VALUES (%s,%s,%s,%s,%s,'tecnica_mari','tecnica_mari_bucket1_5')
                        ON CONFLICT DO NOTHING
                        """,
                        (obra["obra_id"], dec.nome_pessoa, dec.cargo_raw,
                         dec.tipo_cargo, linkedin_url),
                    )
                    if cur.rowcount:
                        total_inseridos += 1
                        inseridos_essa_obra += 1
                        print(f"  +  {dec.nome_pessoa[:30]:30} | {(dec.cargo_raw or '')[:35]:35} | tipo={dec.tipo_cargo} | gate={motivo}")
                else:
                    total_inseridos += 1
                    inseridos_essa_obra += 1
                    print(f"  ?? {dec.nome_pessoa[:30]:30} | {(dec.cargo_raw or '')[:35]:35} | tipo={dec.tipo_cargo}")

    if args.commit:
        conn.commit()

    print(f"\n{'='*60}")
    print(f"  Descobertos brutos:  {total_descobertos}")
    print(f"  Inseridos (ou seriam): {total_inseridos}")
    print(f"  Gate rejeitou:        {total_gate_reject}")
    print(f"  Skip duplicado:       {total_skip_existe}")
    print(f"  Tempo: {time.time()-t0:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
