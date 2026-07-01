"""Frente C — classificação Haiku 4.5 dos single-obra score<10 do enrichment_19_05.

Para cada decisor único (1 obra), Haiku decide se o match decisor↔obra é defensável.
Critério: cargo cita empresa da obra OU empresa da obra é claramente parte do grupo
do decisor. Caso contrário, replicação espúria — não promover.

Dry-run: só imprime decisão. Com --commit: UPDATE confianca_match=75 nos match=true
com linkedin/email — trigger sync_classificacao_after_decisor promove pra OURO.
"""
import argparse
import sys as _scompat
if "/app" not in _scompat.path: _scompat.path.insert(0, "/app")
from services.llm_haiku_compat import _haiku_client, _haiku_async_client  # free-first 25/06
import json
import os
import re
import sys
import time

import psycopg2
import psycopg2.extras

sys.path.insert(0, "/app")
try:
    import anthropic
except ImportError:
    print("install: pip install anthropic")
    sys.exit(1)


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

CANDIDATOS_SQL = """
WITH freq AS (
  SELECT nome, COUNT(*) AS qtd_obras
  FROM decisores_obra
  WHERE registrado_por = 'enrichment_19_05'
    AND excluido_em IS NULL
    AND confianca_match < 10
  GROUP BY nome
)
SELECT
  d.id::text AS dec_id,
  d.nome,
  COALESCE(d.cargo, '') AS cargo,
  COALESCE(d.email, '') AS email,
  COALESCE(d.linkedin_url, '') AS linkedin_url,
  COALESCE(o.empresa, '') AS empresa,
  COALESCE(LEFT(o.descricao, 300), '') AS descricao,
  o.fase,
  o.classificacao_computed AS tier
FROM decisores_obra d
JOIN obras o ON o.id = d.obra_id
JOIN freq f ON f.nome = d.nome
WHERE d.registrado_por = 'enrichment_19_05'
  AND d.excluido_em IS NULL
  AND d.confianca_match < 10
  AND f.qtd_obras = 1
ORDER BY d.nome
"""


PROMPT_SYSTEM = """Você é auditor de matches decisor↔obra. Sua tarefa é decidir se um decisor (nome + cargo) é defensavelmente associado a uma obra (empresa contratante), respondendo APENAS em JSON.

Critério de MATCH (responda match=true):
- O cargo do decisor cita a empresa da obra explicitamente OU
- O cargo do decisor cita uma empresa que é claramente o mesmo grupo econômico da empresa da obra (ex: SPE de um grupo conhecido) OU
- A empresa da obra é a mesma do cargo do decisor (mesma instituição, mesma marca)

Critério de NO MATCH (responda match=false):
- Cargo cita empresa diferente sem relação aparente com a obra
- Empresas pertencem a indústrias/grupos não relacionados
- Empresa da obra está vazia ou é entidade pública genérica (Ministério, Município, Distrito Federal) — não dá pra defender o match

Responda APENAS o JSON, sem ```fences nem texto fora dele:
{"match": true|false, "motivo": "string curta", "confianca": 0-100}"""


def classify_haiku(client, dec, model="claude-haiku-4-5-20251001"):
    user_msg = (
        f"DECISOR\n"
        f"  nome: {dec['nome']}\n"
        f"  cargo: {dec['cargo']}\n"
        f"\n"
        f"OBRA\n"
        f"  empresa: {dec['empresa'] or '(VAZIA)'}\n"
        f"  fase: {dec['fase']}\n"
        f"  descricao: {dec['descricao'][:300] if dec['descricao'] else '(vazia)'}\n"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        system=PROMPT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data, text
    except json.JSONDecodeError:
        return None, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    client = _haiku_client()
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(CANDIDATOS_SQL)
    candidatos = cur.fetchall()
    if args.limit:
        candidatos = candidatos[: args.limit]
    print(f"Frente C: {len(candidatos)} candidatos ({'COMMIT' if args.commit else 'DRY-RUN'})\n")

    match_count = 0
    nomatch_count = 0
    skip_count = 0
    update_ids = []
    t0 = time.time()

    for i, dec in enumerate(candidatos, 1):
        if not dec["empresa"]:
            print(f"[{i:02d}/{len(candidatos)}] SKIP empresa vazia → {dec['nome']}")
            skip_count += 1
            continue
        result, raw = classify_haiku(client, dec)
        if not result:
            print(f"[{i:02d}/{len(candidatos)}] PARSE_ERR {dec['nome']}: {raw[:80]}")
            skip_count += 1
            continue
        m = result.get("match")
        c = result.get("confianca", 0)
        mot = result.get("motivo", "")[:60]
        flag = "MATCH" if m else "no   "
        tem_contato = bool(dec["email"] or dec["linkedin_url"])
        print(
            f"[{i:02d}/{len(candidatos)}] {flag} c={c:3} | "
            f"{dec['nome'][:30]:30} | {dec['cargo'][:30]:30} | "
            f"obra={dec['empresa'][:30]:30} | {mot}"
        )
        if m:
            match_count += 1
            if tem_contato:
                update_ids.append(dec["dec_id"])
        else:
            nomatch_count += 1

    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(f"  MATCH:        {match_count}")
    print(f"  NO MATCH:     {nomatch_count}")
    print(f"  SKIP/erro:    {skip_count}")
    print(f"  Com contato e promovíveis: {len(update_ids)}")
    print(f"  Tempo: {elapsed:.1f}s")

    if args.commit and update_ids:
        cur.execute(
            """
            UPDATE decisores_obra
               SET confianca_match = 75,
                   hipotese_replicacao = 'OK',
                   confianca_match_calculada_em = NOW(),
                   confianca_match_componentes = COALESCE(confianca_match_componentes, '{}'::jsonb)
                                                 || jsonb_build_object('frente_c_haiku', true)
             WHERE id::text = ANY(%s)
            """,
            (update_ids,),
        )
        conn.commit()
        print(f"  UPDATEd {cur.rowcount} decisores → trigger reclassifica obras")
    elif update_ids:
        print(f"  [DRY-RUN] {len(update_ids)} decisores seriam promovidos (rode com --commit)")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
