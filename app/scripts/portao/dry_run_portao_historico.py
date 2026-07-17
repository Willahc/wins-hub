#!/usr/bin/env python3
"""Dry run do Portão v5 sobre public.obras (somente leitura).

Não altera visibilidade, tiers nem status_portao históricos.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, "/app/scripts/portao")
sys.path.insert(0, str(ROOT))

from portao_gate_v5 import decidir_portao, VALOR_MINIMO  # noqa: E402

OUT = ROOT / "relatorios"
OUT.mkdir(parents=True, exist_ok=True)
SAMPLES = ROOT / "samples"
SAMPLES.mkdir(parents=True, exist_ok=True)


def connect():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    return conn, RealDictCursor


def main() -> int:
    conn, RDC = connect()
    stats = Counter()
    by_fonte = defaultdict(Counter)
    by_setor = defaultdict(Counter)
    by_regra = Counter()
    by_conf_bucket = Counter()
    impacto_tier = defaultdict(Counter)
    samples = {"APROVADA": [], "REJEITADA": [], "EM_ANALISE": []}
    ouro_prata_baixo = []
    nao_obra_visivel = []
    all_rows_out = []

    try:
        with conn.cursor(cursor_factory=RDC) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM public.obras")
            total = int(cur.fetchone()["n"])
            print(f"total obras={total}", flush=True)
            cur.execute(
                """
                SELECT id::text, nome, descricao, descricao_publica, setor, fonte, fonte_tipo,
                       valor_estimado, capex_fonte, url_fonte, cnpj, empresa, id_externo,
                       fase, status_licenca, municipio, uf, visivel, classificacao_computed,
                       motivo_invisivel
                  FROM public.obras
                 ORDER BY criado_em NULLS LAST
                """
            )
            batch = 0
            while True:
                rows = cur.fetchmany(500)
                if not rows:
                    break
                for r in rows:
                    batch += 1
                    obra = dict(r)
                    # dry run: não usar conn de dedup real para não travar (opcional)
                    dec = decidir_portao(obra, conn=None)
                    stats[dec.status_portao] += 1
                    by_fonte[obra.get("fonte") or "?"][dec.status_portao] += 1
                    by_setor[obra.get("setor") or "?"][dec.status_portao] += 1
                    by_regra[dec.regra_aplicada] += 1
                    cb = f"{int(dec.confianca*10)/10:.1f}"
                    by_conf_bucket[cb] += 1
                    tier = obra.get("classificacao_computed") or "(null)"
                    impacto_tier[tier][dec.status_portao] += 1

                    valor = obra.get("valor_estimado")
                    if valor is not None and float(valor) < VALOR_MINIMO:
                        stats["abaixo_100k"] += 1
                    if "valor_elegivel_100k" in dec.criterios_ausentes and valor is None:
                        stats["sem_valor_confiavel"] += 1
                    if "intervencao_fisica" in dec.criterios_ausentes:
                        stats["sem_intervencao"] += 1
                    if "ativo_fisico" in dec.criterios_ausentes:
                        stats["sem_ativo"] += 1

                    if (
                        tier in ("OURO", "PRATA")
                        and valor is not None
                        and float(valor) < VALOR_MINIMO
                    ):
                        ouro_prata_baixo.append(_sample_row(obra, dec))
                    if (
                        dec.status_portao == "REJEITADA"
                        and obra.get("visivel")
                        and dec.regra_aplicada.startswith("REJEICAO")
                    ):
                        if len(nao_obra_visivel) < 200:
                            nao_obra_visivel.append(_sample_row(obra, dec))

                    if len(samples[dec.status_portao]) < 120:
                        samples[dec.status_portao].append(_sample_row(obra, dec))

                    if batch <= 5000:  # amostra tabular parcial para inspeção
                        all_rows_out.append(_sample_row(obra, dec))

                if batch % 5000 == 0:
                    print(f"  processadas={batch} ... {dict(stats)}", flush=True)
    finally:
        conn.close()

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "versao": "portao-v5.0.0",
        "total": total,
        "decisoes": {
            "APROVADA": stats.get("APROVADA", 0),
            "REJEITADA": stats.get("REJEITADA", 0),
            "EM_ANALISE": stats.get("EM_ANALISE", 0),
        },
        "abaixo_100k": stats.get("abaixo_100k", 0),
        "sem_valor_confiavel": stats.get("sem_valor_confiavel", 0),
        "sem_intervencao_fisica": stats.get("sem_intervencao", 0),
        "sem_ativo": stats.get("sem_ativo", 0),
        "por_regra": dict(by_regra.most_common()),
        "por_confianca": dict(sorted(by_conf_bucket.items())),
        "impacto_tier_atual": {k: dict(v) for k, v in impacto_tier.items()},
        "por_fonte_top": {
            f: dict(c) for f, c in sorted(by_fonte.items(), key=lambda x: -sum(x[1].values()))[:30]
        },
        "por_setor": {s: dict(c) for s, c in sorted(by_setor.items(), key=lambda x: -sum(x[1].values()))},
        "nota": "DRY RUN — nenhuma alteração no banco. Histórico NÃO aplicado.",
    }

    (OUT / "DRY_RUN_PORTAO_35690.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # Markdown summary
    md = [
        "# Dry run Portão v5 — public.obras",
        "",
        f"- Timestamp: {report['ts']}",
        f"- Total: **{total}**",
        f"- APROVADA: **{report['decisoes']['APROVADA']}**",
        f"- REJEITADA: **{report['decisoes']['REJEITADA']}**",
        f"- EM_ANALISE: **{report['decisoes']['EM_ANALISE']}**",
        f"- Abaixo R$100k: **{report['abaixo_100k']}**",
        f"- Sem valor confiável: **{report['sem_valor_confiavel']}**",
        f"- Sem intervenção física: **{report['sem_intervencao_fisica']}**",
        f"- Sem ativo: **{report['sem_ativo']}**",
        "",
        "## Impacto sobre tiers atuais (classificacao_computed × decisão simulada)",
        "",
        "| Tier atual | APROVADA | REJEITADA | EM_ANALISE |",
        "|---|---:|---:|---:|",
    ]
    for tier, c in sorted(impacto_tier.items(), key=lambda x: -sum(x[1].values())):
        md.append(
            f"| {tier} | {c.get('APROVADA',0)} | {c.get('REJEITADA',0)} | {c.get('EM_ANALISE',0)} |"
        )
    md += [
        "",
        "## Top regras",
        "",
    ]
    for regra, n in by_regra.most_common(20):
        md.append(f"- `{regra}`: {n}")
    md.append("")
    md.append("**Histórico NÃO foi alterado.** `PORTAO_OBRAS_HISTORICAL_ENABLED=false`.")
    (OUT / "DRY_RUN_PORTAO_35690.md").write_text("\n".join(md), encoding="utf-8")

    # Sampling CSVs
    fields = [
        "id", "titulo", "captador", "objeto_original", "valor", "tipo_valor",
        "decisao", "confianca", "motivo", "evidencia", "url_fonte", "tier_atual", "visivel",
    ]
    for status, rows in samples.items():
        path = SAMPLES / f"amostra_{status.lower()}.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows[:100]:
                w.writerow(r)

    with (SAMPLES / "ouro_prata_abaixo_100k.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in ouro_prata_baixo:
            w.writerow(r)

    with (SAMPLES / "nao_obra_visivel_provavel.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in nao_obra_visivel[:100]:
            w.writerow(r)

    sampling = {
        "aprovadas_auto": len(samples["APROVADA"][:100]),
        "rejeitadas_auto": len(samples["REJEITADA"][:100]),
        "em_analise": len(samples["EM_ANALISE"][:100]),
        "ouro_prata_abaixo_100k": len(ouro_prata_baixo),
        "nao_obra_visivel_provavel": len(nao_obra_visivel[:100]),
        "arquivos": [
            "samples/amostra_aprovada.csv",
            "samples/amostra_rejeitada.csv",
            "samples/amostra_em_analise.csv",
            "samples/ouro_prata_abaixo_100k.csv",
            "samples/nao_obra_visivel_provavel.csv",
        ],
    }
    (OUT / "AMOSTRAGEM_PORTAO.json").write_text(
        json.dumps(sampling, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps({"report": report["decisoes"], "sampling": sampling}, ensure_ascii=False, indent=2))
    return 0


def _sample_row(obra, dec):
    ev = dec.evidencias[0] if dec.evidencias else {}
    return {
        "id": obra.get("id"),
        "titulo": (obra.get("nome") or "")[:180],
        "captador": obra.get("fonte") or "",
        "objeto_original": ((obra.get("descricao") or obra.get("descricao_publica") or "")[:300]),
        "valor": obra.get("valor_estimado"),
        "tipo_valor": obra.get("capex_fonte") or "declarado/desconhecido",
        "decisao": dec.status_portao,
        "confianca": dec.confianca,
        "motivo": dec.motivo,
        "evidencia": json.dumps(ev, ensure_ascii=False, default=str)[:300],
        "url_fonte": (obra.get("url_fonte") or "")[:200],
        "tier_atual": obra.get("classificacao_computed") or "",
        "visivel": obra.get("visivel"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
