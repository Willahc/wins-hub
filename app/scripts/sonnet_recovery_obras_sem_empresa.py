#!/usr/bin/env python3
"""Sonnet recovery em obras visivel=true sem empresa.

Para cada obra (descrição + título + URL), pede ao Sonnet 4.6:
  - extrair empresa contratante (se houver)
  - estimar capex real em R$M
  - recomendar tier (OURO/PRATA/BRONZE/PIPELINE/REJEITAR)
  - confiança

Auto-apply rules (conservador):
  - confianca alta + tier REJEITAR  → obras.visivel = false (some da vitrine)
  - confianca alta + empresa preenchida → UPDATE obras.empresa
  - confianca media/baixa → só persiste o JSON (review manual)

Salva auditoria completa em /tmp/data/sonnet_recovery_<data>.json.

Usage:
  python sonnet_recovery_obras_sem_empresa.py [--dry-run] [--limit N]
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import sys

import anthropic
import psycopg2
import psycopg2.extras


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST", "db"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "wins_hub"),
}

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Voce e um analista B2B de obras industriais no Brasil.

Recebe noticia/registro de obra sem `empresa` extraida. Sua tarefa:
1. Identificar empresa contratante da obra (a que paga e contrata fornecedores).
2. Estimar capex contratavel em R$M.
3. Recomendar tier B2B.

Retorne SOMENTE JSON valido. Sem markdown."""

USER_TEMPLATE = """Analise esta obra:

Titulo: {titulo}
Fonte: {fonte}
URL: {url}
Descricao:
{descricao}

Criterios:
1. EMPRESA CONTRATANTE: nome da empresa que ira CONTRATAR servicos/fornecedores. Pode ser anunciante, dona do empreendimento, ou orgao publico. Se for noticia genérica/regulatoria sem empresa especifica, retorne null.
2. CAPEX em R$M: capex contratavel por fornecedores BR. Distinguir de faturamento, valor de leilao, valor de financiamento agregado etc.
3. TIER:
   - OURO: capex >= R$500M + B2B contratavel + decisor identificavel
   - PRATA: capex R$100-500M + B2B contratavel
   - BRONZE: capex R$50-100M + B2B contratavel
   - PIPELINE: capex R$10-50M ou incerteza sobre contratabilidade
   - REJEITAR: nao e obra B2B / capex < R$10M / noticia regulatoria / financeira / consumo

Retorne EXATAMENTE este JSON:
{{
  "empresa": "Nome da empresa contratante" ou null,
  "capex_M": 0,
  "decisor_indicios": "trecho onde menciona decisor (nome+cargo) ou null",
  "tier_recomendado": "OURO|PRATA|BRONZE|PIPELINE|REJEITAR",
  "confianca": "alta|media|baixa",
  "justificativa": "1 paragrafo direto"
}}"""


def analisar(client, obra):
    desc = (obra["descricao"] or "")[:3500]
    prompt = USER_TEMPLATE.format(
        titulo=obra["nome"] or "",
        fonte=obra["fonte"] or "",
        url=obra["url_fonte"] or "",
        descricao=desc,
    )
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=600, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"  JSON malformado: {e}\n  raw: {raw[:300]}")
        return None
    except Exception as e:
        log.error(f"  Erro Sonnet: {type(e).__name__}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Roda Sonnet, salva audit, mas NÃO escreve no DB")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap em N obras (default: todas)")
    args = ap.parse_args()

    client = anthropic.Anthropic()
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("""
        SELECT id::text, nome, fonte, fonte_tipo, url_fonte, descricao,
               classificacao_computed
          FROM obras
         WHERE visivel = true
           AND (empresa IS NULL OR empresa = '')
         ORDER BY criado_em DESC
    """ + (f" LIMIT {args.limit}" if args.limit else ""))
    obras = cur.fetchall()
    log.info(f"obras sem empresa: {len(obras)}")

    resultados = {}
    n_promove = n_rejeita = n_review = n_erro = 0

    for i, obra in enumerate(obras, 1):
        log.info(f"[{i}/{len(obras)}] {obra['id']} | {(obra['nome'] or '')[:70]}")
        analysis = analisar(client, obra)
        if not analysis:
            n_erro += 1
            continue

        resultados[obra["id"]] = {
            "nome": obra["nome"],
            "fonte": obra["fonte"],
            "tier_atual": obra["classificacao_computed"],
            "analysis": analysis,
        }

        conf = (analysis.get("confianca") or "baixa").lower()
        tier_rec = (analysis.get("tier_recomendado") or "PIPELINE").upper()
        empresa = analysis.get("empresa")

        log.info(f"  → tier={tier_rec} conf={conf} emp={empresa}")

        # contadores (mesmo em dry-run, pra preview do impacto)
        if conf == "alta" and tier_rec == "REJEITAR":
            n_rejeita += 1
        elif conf == "alta" and empresa and isinstance(empresa, str) and empresa.strip():
            n_promove += 1
        else:
            n_review += 1

        if args.dry_run:
            continue

        if conf == "alta":
            if tier_rec == "REJEITAR":
                cur.execute("""
                    UPDATE obras
                       SET visivel = false,
                           observacoes_validacao = COALESCE(observacoes_validacao,'') ||
                                                   ' [sonnet_recovery_20260519: REJEITAR]'
                     WHERE id = %s
                """, (obra["id"],))
            elif empresa and isinstance(empresa, str) and empresa.strip():
                cur.execute("""
                    UPDATE obras
                       SET empresa = %s
                     WHERE id = %s
                       AND (empresa IS NULL OR empresa = '')
                """, (empresa.strip()[:200], obra["id"]))

        if i % 10 == 0:
            conn.commit()
            log.info(f"  ── commit parcial em {i}")

    if not args.dry_run:
        conn.commit()

    out_path = f"/tmp/data/sonnet_recovery_{dt.date.today().strftime('%Y%m%d')}.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)

    log.info("=" * 50)
    log.info(f"obras processadas: {len(resultados)}")
    log.info(f"  promove empresa: {n_promove}")
    log.info(f"  rejeita visivel: {n_rejeita}")
    log.info(f"  review manual:   {n_review}")
    log.info(f"  erros llm:       {n_erro}")
    log.info(f"audit: {out_path}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
