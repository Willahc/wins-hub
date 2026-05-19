#!/usr/bin/env python3
"""Wire-in: aplica decisores_empresa_alvo (v3 validated) nas obras.

Para cada lead com nivel1_fonte = 'site_linkedin_serper_v3':
  - Busca obras visivel=true, classificacao IN (tiers), sem nivel1_nome,
    cuja empresa NORMALIZADA bate com a empresa do lead via distinctive token.
  - UPDATE obras SET nivel1_nome/cargo/linkedin + origem='decisores_empresa_alvo'.
  - Skip obras ambíguas (match em >1 lead diferente).

Usage CLI:
  python apply_decisores_empresa_alvo.py [--dry-run] [--tiers OURO,PRATA]

Importável (chamado pelo orchestrator):
  from scripts.apply_decisores_empresa_alvo import executar
  stats = executar(tiers=("OURO","PRATA"), dry_run=False)
"""
import argparse
import os
import re
from collections import Counter, defaultdict
from typing import Iterable

import psycopg2
import psycopg2.extras
from unidecode import unidecode


DB_CONFIG = {
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST", "db"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "wins_hub"),
}

SUFIXOS_CORP = {
    "sa", "s/a", "s.a", "s.a.", "ltda", "ltd", "eireli",
    "grupo", "group", "holding", "companhia", "co",
    "brasil", "brazil", "br",
    "participacoes", "participações", "part",
    "do", "da", "de", "dos", "das", "e",
    "farmaceutica", "farmacêutica", "farma",
    "industria", "indústria", "industrias", "indústrias", "industrial",
    "agro", "agronegocio", "agronegócio",
    "com", "corp", "corporation", "inc",
    "saneamento", "energia", "energias", "logistica", "logística",
    "transportes", "transp",
    "aeroportos", "aeroporto",
}


def _normalize(s):
    return unidecode((s or "").lower())


def _tokens_distintivos(empresa_nome, min_len=5):
    norm = re.sub(r"[^a-z0-9\s]", " ", _normalize(empresa_nome))
    tokens = [t for t in norm.split() if t not in SUFIXOS_CORP and len(t) >= min_len]
    if not tokens:
        tokens = [t for t in norm.split() if t not in SUFIXOS_CORP and len(t) >= 3]
    return tokens


def executar(tiers: Iterable[str] = ("OURO", "PRATA"),
             dry_run: bool = False,
             verbose: bool = True) -> dict:
    """Aplica decisores_empresa_alvo v3-validated nas obras dos tiers dados.

    Retorna dict com stats: {leads, candidatas, matched, ambiguos, atualizadas, por_tier}.
    """
    tiers_t = tuple(tiers)
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    def log(msg):
        if verbose:
            print(msg)

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("""
            SELECT empresa_nome, nivel1_nome, nivel1_cargo, nivel1_linkedin
              FROM decisores_empresa_alvo
             WHERE nivel1_fonte = 'site_linkedin_serper_v3'
               AND nivel1_nome IS NOT NULL
        """)
        leads = cur.fetchall()
        log(f"[wire-in] {len(leads)} leads v3-validated")

        cur.execute("""
            SELECT id, empresa, classificacao_computed
              FROM obras
             WHERE visivel = true
               AND classificacao_computed IN %s
               AND (nivel1_nome IS NULL OR nivel1_nome = '')
               AND empresa IS NOT NULL AND empresa <> ''
        """, (tiers_t,))
        obras = cur.fetchall()
        log(f"[wire-in] {len(obras)} obras candidatas em {tiers_t}")

        obra_para_leads = defaultdict(list)
        for lead in leads:
            tokens = _tokens_distintivos(lead["empresa_nome"])
            if not tokens:
                continue
            tok_principal = max(tokens, key=len)
            for obra in obras:
                obra_norm = re.sub(r"[^a-z0-9\s]", " ", _normalize(obra["empresa"]))
                if re.search(rf"\b{re.escape(tok_principal)}\b", obra_norm):
                    obra_para_leads[obra["id"]].append({
                        "empresa_alvo": lead["empresa_nome"],
                        "tok": tok_principal,
                        "nivel1_nome": lead["nivel1_nome"],
                        "nivel1_cargo": lead["nivel1_cargo"],
                        "nivel1_linkedin": lead["nivel1_linkedin"],
                        "obra_empresa": obra["empresa"],
                        "obra_tier": obra["classificacao_computed"],
                    })

        aplica, ambiguos = [], []
        for obra_id, lst in obra_para_leads.items():
            if len({l["empresa_alvo"] for l in lst}) > 1:
                ambiguos.append(obra_id)
            else:
                aplica.append((obra_id, lst[0]))

        por_tier = Counter(m["obra_tier"] for _, m in aplica)
        log(f"[wire-in] {len(aplica)} obras matched, {len(ambiguos)} ambíguas")
        for t, n in sorted(por_tier.items()):
            log(f"  {t}: {n}")

        n_upd = 0
        if not dry_run:
            for obra_id, m in aplica:
                cur.execute("""
                    UPDATE obras
                       SET nivel1_nome = %s,
                           nivel1_cargo = %s,
                           nivel1_linkedin = %s,
                           nivel1_origem_enrichment = 'decisores_empresa_alvo'
                     WHERE id = %s
                       AND (nivel1_nome IS NULL OR nivel1_nome = '')
                """, (m["nivel1_nome"], m["nivel1_cargo"], m["nivel1_linkedin"], obra_id))
                n_upd += cur.rowcount
            conn.commit()
            log(f"[wire-in] {n_upd} obras UPDATEd")

        return {
            "leads": len(leads),
            "candidatas": len(obras),
            "matched": len(aplica),
            "ambiguos": len(ambiguos),
            "atualizadas": n_upd,
            "por_tier": dict(por_tier),
            "amostra": [
                {"obra_id": str(oid), **m}
                for oid, m in aplica[:10]
            ],
        }
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tiers", default="OURO,PRATA",
                    help="Tiers elegíveis (default OURO,PRATA; BRONZE só com pedido explícito)")
    args = ap.parse_args()
    tiers = tuple(t.strip() for t in args.tiers.split(",") if t.strip())
    executar(tiers=tiers, dry_run=args.dry_run, verbose=True)


if __name__ == "__main__":
    main()
