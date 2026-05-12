"""
promover_pratas_via_hunter.py
Promove obras PRATA → OURO inserindo decisores do cache Hunter em decisores_obra.

Lógica híbrida por obra:
  1. executive com cargo decisor → top-1
  2. sem executive → senior com cargo decisor → top-2
  3. só c-level puro (CEO/COO/President) → top-1
  4. só operacional → skip (obra fica PRATA)

DRY-RUN por padrão. Use --commit pra persistir.

Uso:
    docker exec wins_hub-api-1 python /app/scripts/promover_pratas_via_hunter.py
    docker exec wins_hub-api-1 python /app/scripts/promover_pratas_via_hunter.py --commit
    docker exec wins_hub-api-1 python /app/scripts/promover_pratas_via_hunter.py --limit 10
"""
import sys
sys.path.insert(0, "/app")

import argparse
import json
import re
from collections import defaultdict
import psycopg2
from psycopg2.extras import RealDictCursor

from services.matchmaking import DB_CONFIG


# ── Mapeamento EN → tipo_cargo (enum decisores_obra.tipo_cargo) ─────────────

CARGO_MAP = [
    # (regex pattern, tipo_cargo)
    (r"procurement|purchasing|compras|sourcing",         "GERENTE_SUPRIMENTOS"),
    (r"supply chain|supply manager|suprimentos",         "GERENTE_SUPRIMENTOS"),
    (r"civil eng|structural|geotechnical|geot.cnica",    "ENGENHEIRO_MECANICO_CIVIL"),
    (r"mechanical eng|engenheiro mec",                   "ENGENHEIRO_MECANICO_CIVIL"),
    (r"engineering manager|chief engineer|ger.*eng",     "GERENTE_ENGENHARIA"),
    (r"project manager|project director|ger.*proj",     "GERENTE_PROJETOS"),
    # Director/VP só com qualificador de domínio (ex: "Project Director", "Director of Engineering").
    # "Director" puro vai pra OUTRO — ambíguo demais (HR/Finance/Legal/Statutory).
    (r"\b(?:procurement|supply|civil|mechanical|electrical|geotechnical|structural|engineering|"
     r"project|construction|maintenance|operations|industrial|manufacturing|technical|plant)\s+(?:director|vp|vice.president)\b",  "GERENTE_PROJETOS"),
    (r"\b(?:director|vp|vice.president)\s+of\s+(?:engineering|operations|projects?|construction|maintenance|procurement|supply|industrial|manufacturing|technical|plant)\b",  "GERENTE_PROJETOS"),
    (r"maintenance|manuten",                             "COORDENADOR_MANUTENCAO"),
    (r"industrial|plant manager|f.brica",                "GERENTE_INDUSTRIAL"),
    (r"construction|obras|site manager|canteiro",        "COORDENADOR_OBRAS"),
    (r"commercial|comercial|business dev|bizdev",        "GERENTE_SUPRIMENTOS"),
    # C-level puro — não é decisor de fornecedor direto, mas melhor que nada
    (r"chief executive|ceo\b|president",                 "GERENTE_PROJETOS"),
    (r"chief operating|coo\b",                           "GERENTE_PROJETOS"),
    # Financeiro/TI/RH puro → OUTRO (excluído de OURO)
    (r"chief financial|cfo\b|chief technology|cto\b|"
     r"chief information|cio\b|audit|rh\b|human res|"
     r"talent acqui|recruit",                            "OUTRO"),
]

SENIORITY_EXECUTIVE = {"executive"}
SENIORITY_SENIOR    = {"senior", "executive"}
CLEVEL_PATTERN      = re.compile(r"chief|ceo|coo|president", re.I)


def mapear_cargo(position: str) -> str:
    if not position:
        return "OUTRO"
    p = position.lower()
    for pattern, tipo in CARGO_MAP:
        if re.search(pattern, p, re.I):
            return tipo
    return "OUTRO"


def is_clevel_puro(position: str) -> bool:
    return bool(CLEVEL_PATTERN.search(position or ""))


# ── Selecionar melhor(es) candidato(s) por obra ─────────────────────────────

def selecionar_candidatos(emails_json) -> list:
    """
    Retorna lista de dicts prontos pra inserir em decisores_obra.
    Lógica híbrida:
      1. executives com tipo_cargo != OUTRO → top-1
      2. seniors com tipo_cargo != OUTRO → top-2
      3. c-level puro (CEO/COO/President) → top-1
      4. vazio → []
    """
    if not emails_json:
        return []

    enriched = []
    for e in emails_json:
        position  = e.get("position") or ""
        seniority = (e.get("seniority") or "").lower()
        tipo      = mapear_cargo(position)
        enriched.append({
            "nome":       f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
            "email":      e.get("value") or e.get("email") or "",
            "cargo":      position,
            "tipo_cargo": tipo,
            "seniority":  seniority,
            "confidence": e.get("confidence", 0) or 0,
            "clevel":     is_clevel_puro(position),
        })

    enriched = [e for e in enriched if e["nome"] and e["email"]]

    # Fase 1: executives com cargo decisor
    exec_decisores = [e for e in enriched
                      if e["seniority"] in SENIORITY_EXECUTIVE
                      and e["tipo_cargo"] != "OUTRO"]
    if exec_decisores:
        exec_decisores.sort(key=lambda x: x["confidence"], reverse=True)
        return exec_decisores[:1]

    # Fase 2: seniors com cargo decisor
    senior_decisores = [e for e in enriched
                        if e["seniority"] in SENIORITY_SENIOR
                        and e["tipo_cargo"] != "OUTRO"]
    if senior_decisores:
        senior_decisores.sort(key=lambda x: x["confidence"], reverse=True)
        return senior_decisores[:2]

    # Fase 3: c-level puro (CEO/COO/President)
    clevel = [e for e in enriched if e["clevel"]]
    if clevel:
        clevel.sort(key=lambda x: x["confidence"], reverse=True)
        return clevel[:1]

    return []


# ── SQL ─────────────────────────────────────────────────────────────────────

# Critério canônico de OURO (espelha main.py:120 OURO_DECISOR_SQL)
OURO_CHECK_SQL = """
    SELECT 1 FROM decisores_obra
    WHERE obra_id = %s
      AND tipo_cargo IS NOT NULL AND tipo_cargo <> '' AND tipo_cargo <> 'OUTRO'
      AND ((COALESCE(email,'') <> '') OR (COALESCE(linkedin_url,'') <> ''))
      AND excluido_em IS NULL
    LIMIT 1
"""

INSERT_SQL = """
    INSERT INTO decisores_obra
        (obra_id, nome, cargo, email, tipo_cargo, fonte, registrado_por, registrado_em)
    SELECT %s, %s, %s, %s, %s, 'hunter_io_promote', 'auto:hunter_promote_v1', now()
    WHERE NOT EXISTS (
        SELECT 1 FROM decisores_obra
        WHERE obra_id = %s AND nome = %s AND excluido_em IS NULL
    )
"""

# Candidatas: PRATA (canonical fragment) sem decisor OURO já existente,
# com cache Hunter rico. Evita obras com cnpj hallucinated.
CANDIDATAS_SQL = """
    SELECT o.id, o.nome, o.cnpj, dc.emails, dc.dominio, o.valor_estimado
    FROM obras o
    JOIN decisores_cache dc
      ON regexp_replace(o.cnpj,'[^0-9]','','g') = dc.cnpj
    WHERE COALESCE(o.cnpj_status,'ok') = 'ok'
      AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome <> ''
      AND lower(unaccent(o.nivel1_cargo)) ~
          '(compras|suprimentos|supply|procurement|sourcing|engenh|projetos|obras|manutenc|industrial)'
      AND dc.emails IS NOT NULL
      AND dc.emails::text <> '[]'
      AND jsonb_array_length(dc.emails) > 0
      AND NOT EXISTS (
          SELECT 1 FROM decisores_obra do2
          WHERE do2.obra_id = o.id
            AND do2.tipo_cargo IS NOT NULL
            AND do2.tipo_cargo <> '' AND do2.tipo_cargo <> 'OUTRO'
            AND ((COALESCE(do2.email,'') <> '') OR (COALESCE(do2.linkedin_url,'') <> ''))
            AND do2.excluido_em IS NULL
      )
    ORDER BY o.valor_estimado DESC NULLS LAST
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Persistir inserções (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limitar obras processadas (0=todas)")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    sql = CANDIDATAS_SQL + (f" LIMIT {int(args.limit)}" if args.limit else "")
    cur.execute(sql)
    candidatas = cur.fetchall()

    print(f"\n{'='*70}")
    print(f"  MODO: {'COMMIT' if args.commit else 'DRY-RUN (nada gravado)'}")
    print(f"  Obras candidatas PRATA com cache Hunter: {len(candidatas)}")
    print(f"{'='*70}\n")

    total_inseridos = 0
    total_promovidas = 0
    total_skip = 0
    total_skip_dup = 0
    seen_por_cnpj: dict[str, set[str]] = defaultdict(set)  # cap (cnpj, nome): 1 inserção máxima

    for obra in candidatas:
        emails_raw = obra["emails"]
        try:
            emails = json.loads(emails_raw) if isinstance(emails_raw, str) else emails_raw
        except Exception:
            emails = []

        candidatos = selecionar_candidatos(emails or [])

        if not candidatos:
            total_skip += 1
            print(f"  SKIP  {(obra['nome'] or '')[:55]} — sem cargo decisor no cache")
            continue

        cnpj_norm = re.sub(r"[^0-9]", "", obra["cnpj"] or "")
        candidatos_novos = [c for c in candidatos if c["nome"] not in seen_por_cnpj[cnpj_norm]]

        if not candidatos_novos:
            total_skip_dup += 1
            print(f"  DUP   {(obra['nome'] or '')[:55]} — todos candidatos já inseridos pra esse CNPJ")
            continue

        print(f"  OBRA  {(obra['nome'] or '')[:55]}")
        for c in candidatos_novos:
            print(f"    → {c['nome']:30} | {c['cargo'][:35]:35} "
                  f"| {c['tipo_cargo']:24} | sen={c['seniority']:9} | conf={c['confidence']}")
            if args.commit:
                cur.execute(INSERT_SQL, (
                    obra["id"], c["nome"], c["cargo"], c["email"], c["tipo_cargo"],
                    obra["id"], c["nome"],
                ))
                if cur.rowcount:
                    total_inseridos += 1
            seen_por_cnpj[cnpj_norm].add(c["nome"])

        if args.commit:
            cur.execute(OURO_CHECK_SQL, (obra["id"],))
            if cur.fetchone():
                total_promovidas += 1

    if args.commit:
        conn.commit()
    else:
        conn.rollback()

    print(f"\n{'='*70}")
    print(f"  Inserções:                  {total_inseridos}")
    print(f"  Promovidas PRATA→OURO:      {total_promovidas}")
    print(f"  Skips (sem cargo decisor):  {total_skip}")
    print(f"  Skips (cap CNPJ duplicado): {total_skip_dup}")
    print(f"  Modo: {'COMMIT — persistido' if args.commit else 'DRY-RUN — nada gravado'}")
    print(f"{'='*70}\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
