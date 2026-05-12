#!/usr/bin/env python3
"""Smoke test integrado da Camada 3 (descoberta de decisores)."""
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[2])
if _APP not in sys.path:
    sys.path.insert(0, _APP)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("smoke3")

from sales_intelligence.camada3_decisores.orquestrador import descobrir_decisores

EMPRESAS_DEFAULT = [
    ("32161500000100", "CCR ViaSul"),
    ("44067725000172", "Via Brasil BR-163"),
    ("29884545000190", "EcoRioMinas"),
    ("33000167000101", "Petrobras"),
    ("33337122000127", "Ipiranga"),
]


def run_one(cnpj, empresa, force_refresh=True, max_buckets=2):
    print("\n" + "=" * 78)
    print(f"  CNPJ: {cnpj}  |  Empresa: {empresa}")
    print("=" * 78)
    t0 = time.time()
    try:
        decisores = descobrir_decisores(
            cnpj=cnpj, empresa_nome=empresa,
            force_refresh=force_refresh,
            max_buckets_linkedin=max_buckets,
            habilitar_cvm=True, habilitar_dou=True, habilitar_crea=True,
        )
    except Exception as e:
        print(f"  ❌ orquestrador falhou: {e}")
        return {"cnpj": cnpj, "erro": str(e)}

    elapsed = time.time() - t0
    print(f"\n  Total: {len(decisores)} decisores em {elapsed:.1f}s")

    # distribuicoes
    by_tipo = Counter(d.tipo_cargo for d in decisores)
    by_conf = Counter(d.confianca for d in decisores)
    by_fonte = Counter(d.fonte_descoberta for d in decisores)
    print(f"\n  Por tipo_cargo:")
    for k, v in by_tipo.most_common():
        print(f"    {k:30s} {v}")
    print(f"\n  Por confianca: {dict(by_conf)}")
    print(f"  Por fonte_descoberta: {dict(by_fonte)}")

    # top 5 score
    print(f"\n  Top 5 score_relevancia:")
    for d in decisores[:5]:
        slug = f" /{d.linkedin_slug}" if d.linkedin_slug else ""
        print(f"    [{d.score_relevancia:.2f}] {d.tipo_cargo:25s} {d.nome_pessoa:30s} {d.cargo_raw[:40]}{slug}")

    # anti-alucinacao check: todo decisor deve ter snippet
    sem_snippet = [d for d in decisores if not d.snippet_origem]
    if sem_snippet:
        print(f"\n  ⚠️ ANTI-ALUCINACAO ALERT: {len(sem_snippet)} decisores sem snippet_origem")

    return {
        "cnpj": cnpj, "empresa": empresa,
        "tempo_s": round(elapsed, 1),
        "total": len(decisores),
        "by_tipo": dict(by_tipo),
        "by_conf": dict(by_conf),
        "by_fonte": dict(by_fonte),
        "sem_snippet": len(sem_snippet),
    }


def main():
    args = sys.argv[1:]
    if args:
        casos = [(c, c) for c in args]
    else:
        casos = EMPRESAS_DEFAULT

    resultados = []
    for cnpj, empresa in casos:
        try:
            r = run_one(cnpj, empresa)
            resultados.append(r)
        except KeyboardInterrupt:
            print("Interrompido")
            break
        # delay entre empresas (anti-rate-limit)
        if cnpj != casos[-1][0]:
            print(f"\n  Aguardando 30s antes da proxima empresa...")
            time.sleep(30)

    print("\n" + "=" * 78)
    print("  RESUMO CONSOLIDADO")
    print("=" * 78)
    for r in resultados:
        if "erro" in r:
            print(f"  {r['empresa']:20s} ERRO: {r['erro'][:80]}")
            continue
        print(f"  {r['empresa']:20s}  total={r['total']:3d}  conf={r['by_conf']}  fontes={r['by_fonte']}")

    out_json = "/tmp/smoke_camada3_resultado.json"
    with open(out_json, "w") as f:
        json.dump(resultados, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Resultado JSON: {out_json}")


if __name__ == "__main__":
    sys.exit(main())
