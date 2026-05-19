#!/usr/bin/env python3
"""Score v3 para decisores_empresa_alvo.

Gates v3 sobre v2:
- HARD: empresa pesquisada precisa estar no TITLE (não só snippet)
- HARD: cargo precisa normalizar pra tipo != None via mapping.normalizar_cargo
- Penalty -10 se outra empresa-alvo (competitor) aparece no title
- Bonus +5 quando passou GATE 1

Re-rodando nos 2 JSONs originais (data/empresas_alvo_*.json + data/mari_serper_*.json),
re-elege o melhor lead por empresa e UPDATE em decisores_empresa_alvo:
  - nivel1_fonte = 'site_linkedin_serper_v3'    quando v3 achou candidato válido
  - nivel1_fonte = 'site_linkedin_serper_v3_flagged'   quando todos rejeitados (clear nivel1_*)

Saída adicional: data/score_v3_results_20260519.json com auditoria full.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

import psycopg2
import psycopg2.extras
from unidecode import unidecode

from sales_intelligence.camada3_decisores.mapping import normalizar_cargo


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
    "aeroportos", "aeroporto",  # aparece em 4+ empresas-alvo
}

BONUS_3 = ["director", "diretor", "head of", "cpo", "cso", "chief", "vice president"]
BONUS_3_WORD = ["vp"]  # match como palavra inteira (não substring)
BONUS_2 = ["procurement", "supply chain", "suprimentos", "compras", "sourcing",
           "capex", "purchasing", "logistica", "logística"]
BONUS_1 = ["gerente", "manager", "coordenador", "coordinator", "specialist",
           "analyst", "analista", "engenheiro", "engineer"]

# Palavras-chave que justificam aceitar mesmo quando normalizar_cargo() retorna None/OUTRO.
# Combinação cargo-nivel + área-funcional típica de procurement/supply chain.
PALAVRAS_FUNCIONAIS_VALIDAS = [
    "procurement", "supply chain", "suprimentos", "sourcing", "compras",
    "capex", "purchasing", "logística", "logistica",
]
PALAVRAS_NIVEL_VALIDAS = [
    "diretor", "director", "head", "gerente", "manager",
    "coordenador", "coordinator", "vp", "vice president", "cpo",
]
PENALTY = ["marketing", "financ", "juridic", "jurídic", "legal",
           "recursos humanos", " rh ", " hr ", "black belt",
           "comunicacao", "comunicação", "communications",
           "sales", "vendas", "mkt",
           "controllership", "controller", "auditor", "tributario", "tributário",
           "impostos", "tax", "treasury", "investor relations", "ri ",
           "academic", "estudante", "student", "intern", "estagiari",
           "relationship manager",  # bancos
           "advogado", "advogada", "attorney",
           "medico", "médico", "enfermeir",
           "professor", "lecturer"]


def normalize(s: str) -> str:
    return unidecode((s or "").lower())


def tokens_empresa(empresa: str):
    norm = normalize(empresa)
    norm = re.sub(r"[^a-z0-9\s]", " ", norm)
    return [t for t in norm.split() if t not in SUFIXOS_CORP and len(t) >= 3]


def construir_distintivos(todas_empresas):
    """Token é 'distintivo' se aparece em ≤2 empresas do universo."""
    cnt = Counter()
    tokens_por_emp = {}
    for e in todas_empresas:
        tks = tokens_empresa(e)
        tokens_por_emp[e] = tks
        for t in set(tks):
            cnt[t] += 1
    distintivos = {}
    for e, tks in tokens_por_emp.items():
        dist = [t for t in tks if cnt[t] <= 2]
        if not dist:
            dist = tks  # fallback se tudo não-distintivo
        distintivos[e] = dist
    return distintivos


def empresa_no_title(empresa: str, title: str, distintivos: dict) -> bool:
    title_n = re.sub(r"[^a-z0-9\s]", " ", normalize(title))
    for tk in distintivos.get(empresa, []):
        if re.search(rf"\b{re.escape(tk)}\b", title_n):
            return True
    return False


def competitor_no_title(empresa: str, title: str, todas_empresas, distintivos):
    """Checa empresa concorrente no title. Restringe ao trecho APÓS o nome da pessoa
    (após o primeiro ' - ' ou '|'), pra evitar disparar em sobrenomes (Oliveira, Silva).
    Só conta tokens com ≥5 chars.
    """
    m = re.search(r"\s[-|·–]\s(.+)$", title)
    trecho = m.group(1) if m else title
    trecho_n = re.sub(r"[^a-z0-9\s]", " ", normalize(trecho))
    tks_minha = set(distintivos.get(empresa, []))
    for outra in todas_empresas:
        if outra == empresa:
            continue
        for tk in distintivos.get(outra, []):
            if len(tk) < 5:
                continue
            if tk in tks_minha:
                continue
            if re.search(rf"\b{re.escape(tk)}\b", trecho_n):
                return outra
    return None


_NOME_HEAD_RE = re.compile(r"^([^|·\-–]+?)(?:\s[-|·–]\s|\s\|\s)")


def extrair_nome(title: str):
    m = _NOME_HEAD_RE.match(title.strip())
    if not m:
        return None
    nome = m.group(1).strip()
    partes = nome.split()
    if not (2 <= len(partes) <= 5):
        return None
    if any(c.isdigit() for c in nome):
        return None
    # rejeita tokens lixo na primeira posição
    if normalize(partes[0]) in {"em", "no", "na", "de", "da", "do", "com"}:
        return None
    return nome


def extrair_cargo(title: str, snippet: str):
    # tenta tudo entre o '-' depois do nome e o final
    m = re.match(r"^[^|·\-–]+?\s[-|·–]\s(.+)$", title.strip())
    if m:
        cargo = m.group(1).strip()
        cargo = re.sub(r"\s*\|\s*LinkedIn.*$", "", cargo, flags=re.IGNORECASE)
        if cargo and len(cargo) > 2:
            return cargo[:120]
    return (snippet[:120] or "").strip()


def score_hit(empresa, hit, distintivos, todas_empresas):
    title = hit.get("title") or ""
    snippet = hit.get("snippet") or ""
    txt_lower = normalize(f"{title} {snippet}")

    # GATE 1: empresa pesquisada precisa estar no TITLE
    if not empresa_no_title(empresa, title, distintivos):
        return None

    cargo_raw = extrair_cargo(title, snippet)
    cargo_low_check = normalize(cargo_raw)

    tipo = normalizar_cargo(cargo_raw)

    # GATE 2: cargo válido — passa se normalizar_cargo retorna tipo != None & != OUTRO,
    # OU se cargo combina palavra-chave funcional (procurement/supply chain/etc) com nivel
    # (diretor/manager/gerente/etc). Cobre cargos compostos tipo "Diretor de Procurement
    # & Supply Chain" que o mapping não pega via substring.
    tem_funcional = any(p in cargo_low_check for p in PALAVRAS_FUNCIONAIS_VALIDAS)
    tem_nivel = any(p in cargo_low_check for p in PALAVRAS_NIVEL_VALIDAS)
    cargo_aceito_via_keywords = tem_funcional and tem_nivel

    if (tipo is None or tipo == "OUTRO") and not cargo_aceito_via_keywords:
        return None

    nome = extrair_nome(title)
    if not nome:
        return None

    # Penalty extra: se 2º token do nome == token distintivo da empresa, suspeito
    # (ex.: "Maria Eneva" pra empresa Eneva — sobrenome da pessoa, não vínculo).
    nome_tokens = [normalize(t) for t in nome.split()]
    if len(nome_tokens) >= 2:
        for tk in distintivos.get(empresa, []):
            if tk in nome_tokens:
                return None  # rejeita: empresa virou sobrenome

    cargo_low = normalize(cargo_raw)
    score = 0
    if any(t in cargo_low for t in BONUS_3) or \
       any(re.search(rf"\b{w}\b", cargo_low) for w in BONUS_3_WORD):
        score += 3
    if any(t in cargo_low for t in BONUS_2):
        score += 2
    if any(t in cargo_low for t in BONUS_1):
        score += 1
    pen_hit = None
    for p in PENALTY:
        if p in cargo_low or p in txt_lower[:240]:
            score -= 5
            pen_hit = p
            break

    score += 5  # bonus v3 gate passou

    competitor = competitor_no_title(empresa, title, todas_empresas, distintivos)
    if competitor:
        score -= 10

    return {
        "score": score,
        "nome": nome,
        "cargo": cargo_raw[:180],
        "tipo_cargo": tipo,
        "slug": hit.get("slug"),
        "title": title[:220],
        "penalty_hit": pen_hit,
        "competitor": competitor,
    }


def carregar_hits(brand_path, mari_path):
    """Retorna dict empresa -> List[hit] unificado."""
    hits_por_empresa = {}
    with open(brand_path) as f:
        brand = json.load(f)
    for emp, lst in brand.items():
        hits_por_empresa.setdefault(emp, []).extend(lst)
    with open(mari_path) as f:
        mari = json.load(f)
    for emp, sub in mari.items():
        if isinstance(sub, dict):
            for bucket, lst in sub.items():
                hits_por_empresa.setdefault(emp, []).extend(lst)
        elif isinstance(sub, list):
            hits_por_empresa.setdefault(emp, []).extend(sub)
    return hits_por_empresa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="/tmp/data/empresas_alvo_20260518.json")
    ap.add_argument("--mari",  default="/tmp/data/mari_serper_run_20260518.json")
    ap.add_argument("--out",   default="/tmp/data/score_v3_results_20260519.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-score", type=int, default=5,
                    help="Score mínimo pra considerar válido (default 5)")
    args = ap.parse_args()

    hits_por_empresa = carregar_hits(args.brand, args.mari)
    todas = list(hits_por_empresa.keys())
    distintivos = construir_distintivos(todas)
    print(f"[v3] universo: {len(todas)} empresas")

    resultados = {}
    sumario = {"validos": 0, "flagged": 0, "swapped": 0, "kept": 0}

    for empresa, hits in hits_por_empresa.items():
        scored = []
        for h in hits:
            s = score_hit(empresa, h, distintivos, todas)
            if s:
                scored.append(s)
        scored.sort(key=lambda x: x["score"], reverse=True)
        melhor = scored[0] if scored else None
        valido = bool(melhor and melhor["score"] >= args.min_score)
        resultados[empresa] = {
            "melhor": melhor,
            "candidatos_validos": len(scored),
            "valido": valido,
            "todos_scores": [{"nome": s["nome"], "cargo": s["cargo"], "score": s["score"],
                              "title": s["title"]} for s in scored[:5]],
        }
        if valido:
            sumario["validos"] += 1
        else:
            sumario["flagged"] += 1

    print(f"[v3] válidos={sumario['validos']}  flagged={sumario['flagged']}")

    # Persiste auditoria
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)
    print(f"[v3] auditoria salva: {args.out}")

    if args.dry_run:
        print("[v3] DRY-RUN — DB intocado")
        return

    # UPDATE no banco
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT empresa_nome, nivel1_nome, nivel1_cargo FROM decisores_empresa_alvo")
    atuais = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    for empresa, res in resultados.items():
        if empresa not in atuais:
            continue
        if res["valido"]:
            m = res["melhor"]
            nome_atual, _ = atuais[empresa]
            new_fonte = "site_linkedin_serper_v3"
            if nome_atual != m["nome"]:
                sumario["swapped"] += 1
            else:
                sumario["kept"] += 1
            cur.execute("""
                UPDATE decisores_empresa_alvo
                   SET nivel1_nome = %s,
                       nivel1_cargo = %s,
                       nivel1_linkedin = %s,
                       nivel1_fonte = %s,
                       atualizado_em = NOW()
                 WHERE empresa_nome = %s
            """, (
                m["nome"],
                m["cargo"][:200],
                f"https://www.linkedin.com/in/{m['slug']}/" if m.get("slug") else None,
                new_fonte,
                empresa,
            ))
        else:
            cur.execute("""
                UPDATE decisores_empresa_alvo
                   SET nivel1_nome = NULL,
                       nivel1_cargo = NULL,
                       nivel1_linkedin = NULL,
                       nivel1_fonte = 'site_linkedin_serper_v3_flagged',
                       atualizado_em = NOW()
                 WHERE empresa_nome = %s
            """, (empresa,))

    conn.commit()
    cur.close()
    conn.close()
    print(f"[v3] UPDATE done — swapped={sumario['swapped']}  kept={sumario['kept']}  "
          f"flagged={sumario['flagged']}")


if __name__ == "__main__":
    main()
