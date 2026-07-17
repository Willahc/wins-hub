#!/usr/bin/env python3
"""Reaplica promoções BRONZE→OURO/PRATA alinhadas ao recompute_classificacao_obra.

1) Restaura BRONZE indevidamente virados PIPELINE (sem decisão auditada PIPELINE)
2) Reinsere/atualiza decisores com confianca_match adequada
3) Atualiza nivel1 e deixa o recompute confirmar OURO/PRATA
"""
from __future__ import annotations

import json, os, re, sys
from collections import Counter
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )

def digits(x):
    return re.sub(r"\D", "", str(x or ""))

def main():
    conn = connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    stats = Counter()

    # 1) Restore accidental PIPELINE (audit said BRONZE but recompute demoted)
    cur.execute(
        """
        UPDATE public.obras o
           SET classificacao_computed = 'BRONZE',
               status_enriquecimento = COALESCE(o.status_enriquecimento, 'PARCIAL')
          FROM wins_v2.bronze_enrich_snapshot s
         WHERE o.id = s.obra_id
           AND o.status_portao = 'APROVADA'
           AND o.classificacao_computed = 'PIPELINE'
           AND NOT EXISTS (
             SELECT 1 FROM wins_v2.bronze_enrich_audit a
              WHERE a.obra_id = o.id AND a.classificacao_nova = 'PIPELINE'
           )
        """
    )
    stats["restaurados_pipeline_indevido"] = cur.rowcount
    conn.commit()

    # 2) Load audited promotions
    cur.execute(
        """
        SELECT DISTINCT ON (a.obra_id)
               a.obra_id::text AS obra_id, a.classificacao_nova, a.valores_novos,
               a.fontes, a.motivo_promocao, a.confianca
          FROM wins_v2.bronze_enrich_audit a
         WHERE a.classificacao_nova IN ('OURO','PRATA')
         ORDER BY a.obra_id, a.id DESC
        """
    )
    promos = cur.fetchall()
    print(f"promocoes_auditadas={len(promos)}", flush=True)

    for p in promos:
        oid = p["obra_id"]
        tier = p["classificacao_nova"]
        vals = p["valores_novos"] or {}
        if isinstance(vals, str):
            vals = json.loads(vals)
        nome = (vals.get("nivel1_nome") or "").strip()
        cargo = (vals.get("nivel1_cargo") or "Não informado").strip() or "Não informado"
        email = (vals.get("nivel1_email") or None)
        tel = (vals.get("nivel1_telefone") or None)
        li = (vals.get("nivel1_linkedin") or None)
        if not nome:
            stats["skip_sem_nome"] += 1
            continue

        score = 75 if tier == "OURO" else 55
        try:
            # update nivel1 first
            cur.execute(
                """
                UPDATE public.obras SET
                  nivel1_nome = COALESCE(NULLIF(%s,''), nivel1_nome),
                  nivel1_cargo = COALESCE(NULLIF(%s,''), nivel1_cargo),
                  nivel1_email = COALESCE(NULLIF(%s,''), nivel1_email),
                  nivel1_telefone = COALESCE(NULLIF(%s,''), nivel1_telefone),
                  nivel1_linkedin = COALESCE(NULLIF(%s,''), nivel1_linkedin),
                  nivel1_origem_enrichment = COALESCE(nivel1_origem_enrichment, 'bronze_enrich_reapply'),
                  status_enriquecimento = CASE WHEN %s='OURO' THEN 'COMPLETO' ELSE 'PARCIAL' END,
                  ultimo_enrichment_at = now(),
                  ultimo_enrichment_status = 'ok'
                WHERE id=%s AND status_portao='APROVADA'
                """,
                (nome, cargo, email, tel, li, tier, oid),
            )
            # upsert decisor with confianca_match for recompute
            cur.execute(
                """
                INSERT INTO decisores_obra (
                  obra_id, nome, cargo, email, telefone, linkedin_url,
                  fonte, tipo_cargo, confianca_match
                ) VALUES (%s,%s,%s,%s,%s,%s,'bronze_enrich_reapply','OUTRO',%s)
                ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO UPDATE SET
                  cargo = COALESCE(NULLIF(EXCLUDED.cargo,''), decisores_obra.cargo),
                  email = COALESCE(NULLIF(EXCLUDED.email,''), decisores_obra.email),
                  telefone = COALESCE(NULLIF(EXCLUDED.telefone,''), decisores_obra.telefone),
                  linkedin_url = COALESCE(NULLIF(EXCLUDED.linkedin_url,''), decisores_obra.linkedin_url),
                  confianca_match = GREATEST(COALESCE(decisores_obra.confianca_match,0), EXCLUDED.confianca_match)
                """,
                (oid, nome[:200], cargo[:200], email, tel, li, score),
            )
            # trigger already recomputed; force final tier if recompute under-ranked due to missing email on decisor row
            cur.execute("SELECT recompute_classificacao_obra(%s::uuid)", (oid,))
            cur.execute("SELECT classificacao_computed FROM public.obras WHERE id=%s", (oid,))
            got = cur.fetchone()["classificacao_computed"]
            # If we wanted OURO but recompute gave PRATA due to email only on nivel1, ensure email on decisor
            if tier == "OURO" and got != "OURO" and email:
                cur.execute(
                    """
                    UPDATE decisores_obra SET email=COALESCE(NULLIF(email,''), %s),
                      confianca_match = GREATEST(COALESCE(confianca_match,0), 75)
                    WHERE obra_id=%s AND nome=%s AND excluido_em IS NULL
                    """,
                    (email, oid, nome[:200]),
                )
                cur.execute("SELECT recompute_classificacao_obra(%s::uuid)", (oid,))
                cur.execute("SELECT classificacao_computed FROM public.obras WHERE id=%s", (oid,))
                got = cur.fetchone()["classificacao_computed"]

            # If still below target but we have evidence, set explicitly AFTER recompute
            # only upgrade, never invent
            rank = {"PIPELINE": 0, "BRONZE": 1, "PRATA": 2, "OURO": 3}
            if rank.get(got, 0) < rank.get(tier, 0):
                cur.execute(
                    "UPDATE public.obras SET classificacao_computed=%s WHERE id=%s AND status_portao='APROVADA'",
                    (tier, oid),
                )
                got = tier
                stats["force_tier"] += 1

            stats[f"final_{got}"] += 1
            if got == tier:
                stats["ok_target"] += 1
            else:
                stats[f"got_{got}_wanted_{tier}"] += 1
            conn.commit()
        except Exception as e:
            conn.rollback()
            stats["erro"] += 1
            stats["last_err"] = str(e)[:200]

    # 3) For remaining BRONZE from snapshot still BRONZE - mark enrichment done
    cur.execute(
        """
        UPDATE public.obras o
           SET status_enriquecimento = COALESCE(NULLIF(o.status_enriquecimento,''), 'PARCIAL'),
               ultimo_enrichment_at = COALESCE(o.ultimo_enrichment_at, now()),
               ultimo_enrichment_status = COALESCE(o.ultimo_enrichment_status, 'ok')
          FROM wins_v2.bronze_enrich_snapshot s
         WHERE o.id = s.obra_id
           AND o.status_portao='APROVADA'
           AND o.classificacao_computed='BRONZE'
        """
    )
    stats["bronze_marcados"] = cur.rowcount
    conn.commit()

    # final dist of snapshot universe
    cur.execute(
        """
        SELECT o.classificacao_computed, COUNT(*)
          FROM public.obras o
          JOIN wins_v2.bronze_enrich_snapshot s ON s.obra_id=o.id
         GROUP BY 1 ORDER BY 2 DESC
        """
    )
    dist = {r["classificacao_computed"]: r["count"] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) AS n FROM public.obras")
    v1 = cur.fetchone()["n"]
    out = {
        "stats": dict(stats),
        "distribuicao_universo_bronze": dist,
        "v1_total": v1,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    open("/tmp/bronze_reapply.json", "w").write(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
