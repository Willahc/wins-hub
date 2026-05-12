"""
Benchmark cronometrado do pipeline de enriquecimento.

Mede 10 obras-ouro sem decisor completo, separando:
  1. Query banco
  2. Inferência de padrão (se disponível em fornecedor_meta)
  3. Intel comercial (hackertarget — único network local de fato)
  4. Persist (UPDATE)

WebSearch por decisor NÃO roda local — só via remote agent / routine. Tempo
real desse trecho vem dos logs das routines anteriores (anotado no relatório).
"""
from __future__ import annotations

import os
import sys
import time
import logging
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, "/app")
from services.recon_intel import buscar_subdominios, derivar_tags  # type: ignore

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
logging.basicConfig(level=logging.WARNING)


def conn():
    return psycopg2.connect(**DB_CONFIG)


@contextmanager
def timer():
    s = time.perf_counter()
    yield lambda: time.perf_counter() - s


def benchmark(n: int = 10):
    print(f"\n=== BENCHMARK PIPELINE ({n} obras-ouro sem email completo) ===\n")
    fases = {"query_obras": 0.0, "inferencia": 0.0, "intel_hackertarget": 0.0, "persist_dryrun": 0.0}
    sucessos_inferencia = 0
    sucessos_intel = 0
    obras_amostra = []

    # Fase 1: query banco
    with timer() as t:
        c = conn()
        try:
            with c.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT d.id::text AS decisor_id, d.obra_id::text, d.nome, d.cargo,
                           o.empresa, o.cnpj,
                           fm.dominio_email, fm.padrao_email
                    FROM decisores_obra d
                    JOIN obras o            ON o.id = d.obra_id
                    LEFT JOIN fornecedor_meta fm ON fm.cnpj = o.cnpj
                    WHERE d.excluido_em IS NULL
                      AND COALESCE(d.email,'') = ''
                      AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
                      AND (COALESCE(o.nivel1_email,'') != '' OR COALESCE(o.nivel1_linkedin,'') != '')
                      AND cargo_decisor_keyword(o.nivel1_cargo)
                    ORDER BY o.empresa, d.nome
                    LIMIT %s
                    """,
                    (n,),
                )
                obras_amostra = cur.fetchall()
        finally:
            c.close()
    fases["query_obras"] = t()
    print(f"[1] Query DB ({len(obras_amostra)} candidatos)        : {fases['query_obras']*1000:.1f} ms")

    if not obras_amostra:
        print("Sem candidatos. Abortando.")
        return

    # Fase 2: inferência (mesma fórmula do endpoint)
    import re, unicodedata
    def _norm(s):
        s = unicodedata.normalize("NFKD", s or "").encode("ASCII","ignore").decode("ASCII")
        return re.sub(r"[^a-z0-9]", "", s.lower())
    def _gerar(nome, dom, padrao):
        if not all([nome, dom, padrao]) or padrao == "outro": return None
        toks = nome.strip().split()
        if not toks: return None
        primeiro = _norm(toks[0]); ultimo = _norm(toks[-1]) if len(toks)>1 else ""
        if not primeiro: return None
        ini = primeiro[0]
        if padrao == "primeironome": return f"{primeiro}@{dom.lower()}"
        if not ultimo: return None
        m = {"nome.sobrenome":f"{primeiro}.{ultimo}","nome_sobrenome":f"{primeiro}_{ultimo}",
             "nomesobrenome":f"{primeiro}{ultimo}","inicial.sobrenome":f"{ini}.{ultimo}",
             "inicial_sobrenome":f"{ini}_{ultimo}","inicialsobrenome":f"{ini}{ultimo}"}
        return f"{m[padrao]}@{dom.lower()}" if padrao in m else None

    with timer() as t:
        for o in obras_amostra:
            email = _gerar(o["nome"], o["dominio_email"], o["padrao_email"])
            if email:
                sucessos_inferencia += 1
    fases["inferencia"] = t()
    print(f"[2] Inferência padrão (todas {len(obras_amostra)})       : {fases['inferencia']*1000:.1f} ms"
          f"  → {sucessos_inferencia} sucesso(s)")

    # Fase 3: intel hackertarget — 1 chamada por empresa única (não por obra)
    empresas_unicas = {}
    for o in obras_amostra:
        if o["dominio_email"]:
            empresas_unicas.setdefault(o["dominio_email"], (o["cnpj"], o["empresa"]))

    print(f"[3] Intel hackertarget ({len(empresas_unicas)} empresas únicas):")
    with timer() as t_total:
        for dom, (cnpj, emp) in empresas_unicas.items():
            with timer() as t_emp:
                subs, erro = buscar_subdominios(dom)
            tags = derivar_tags(subs, dom) if subs else []
            print(f"     {dom:30} {t_emp()*1000:6.0f} ms  subs={len(subs)} tags={len(tags)} "
                  f"{'erro=' + erro if erro else ''}")
            if subs and not erro:
                sucessos_intel += 1
    fases["intel_hackertarget"] = t_total()
    print(f"     TOTAL                                              {fases['intel_hackertarget']*1000:.0f} ms")

    # Fase 4: persist dry-run (medindo só o tempo de UPDATE simulado)
    with timer() as t:
        c = conn()
        try:
            with c.cursor() as cur:
                # Roda um SELECT que mimica o overhead de UPDATE...WHERE
                for o in obras_amostra:
                    cur.execute(
                        "SELECT id FROM decisores_obra WHERE id = %s::uuid",
                        (o["decisor_id"],),
                    )
                    cur.fetchone()
        finally:
            c.close()
    fases["persist_dryrun"] = t()
    print(f"[4] Persist (overhead pra {len(obras_amostra)} UPDATEs)     : {fases['persist_dryrun']*1000:.1f} ms")

    # Resumo
    total = sum(fases.values())
    print(f"\n=== TOTAL LOCAL: {total*1000:.0f} ms para {len(obras_amostra)} obras "
          f"({(total/len(obras_amostra))*1000:.0f} ms/obra) ===")
    print(f"Inferência success rate: {sucessos_inferencia}/{len(obras_amostra)} "
          f"({100*sucessos_inferencia/len(obras_amostra):.0f}%)")
    print(f"Intel success rate:      {sucessos_intel}/{len(empresas_unicas)} empresas "
          f"({100*sucessos_intel/max(1,len(empresas_unicas)):.0f}%)")


if __name__ == "__main__":
    benchmark(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
