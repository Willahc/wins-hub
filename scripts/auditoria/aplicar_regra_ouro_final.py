#!/usr/bin/env python3
"""Regra definitiva OURO: 8 critérios cumulativos; telefone é apenas bônus.

OURO exige:
1. status_portao = APROVADA
2. CNPJ válido (14 dígitos)
3. domínio corporativo (do e-mail validado, não pessoal)
4. CAPEX/valor elegível > 0
5. nome do decisor
6. cargo compatível (não vazio)
7. LinkedIn do decisor confirmado
8. e-mail corporativo NOMINAL do decisor VALIDADO

Não inventa dados. Não promove por telefone/LinkedIn isolado/e-mail genérico.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch

REGRA = "OURO_8_CRITERIOS_V1"
DRY_RUN = "--dry-run" in sys.argv or os.getenv("DRY_RUN", "") == "1"
APPLY = "--apply" in sys.argv or os.getenv("APPLY", "") == "1"

GENERIC_LOCAL = {
    "contato", "sac", "ouvidoria", "info", "informacoes", "informacoes",
    "noreply", "no-reply", "admin", "suporte", "financeiro", "rh",
    "atendimento", "comercial", "vendas", "secretaria", "protocolo",
    "compras", "juridico", "legal", "marketing", "imprensa", "comunicacao",
    "comunicacao", "recepcao", "geral", "empresa", "office", "mail",
    "webmaster", "ti", "helpdesk", "nfe", "nf", "fiscal", "contabilidade",
}

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.br",
    "outlook.com", "outlook.com.br", "live.com", "live.com.br",
    "yahoo.com", "yahoo.com.br", "icloud.com", "me.com", "aol.com",
    "protonmail.com", "proton.me", "uol.com.br", "bol.com.br",
    "terra.com.br", "ig.com.br", "zipmail.com.br", "msn.com",
}

EMAIL_VALID_STATUS = {
    "valid", "valido", "válido", "ok", "smtp_ok", "verified",
    "verificado_manual", "verificado_manual_osint",
}
EMAIL_INVALID_STATUS = {"invalid", "invalido", "inválido", "bounce", "false"}


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "")
        if unicodedata.category(c) != "Mn"
    )


def norm_token(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def cnpj_valido(cnpj: Any) -> bool:
    d = digits(cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False
    # basic check digit
    def calc(nums, weights):
        s = sum(int(n) * w for n, w in zip(nums, weights))
        r = s % 11
        return "0" if r < 2 else str(11 - r)
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    if calc(d[:12], w1) != d[12] or calc(d[:13], w2) != d[13]:
        return False
    return True


def email_parts(email: str) -> Tuple[str, str]:
    em = (email or "").strip().lower()
    if "@" not in em:
        return "", ""
    local, domain = em.rsplit("@", 1)
    return local, domain


def is_personal_domain(domain: str) -> bool:
    return (domain or "").lower() in PERSONAL_DOMAINS


def is_generic_local(local: str) -> bool:
    loc = (local or "").split("+")[0].lower()
    loc = loc.split(".")[0] if loc in GENERIC_LOCAL else loc
    if loc in GENERIC_LOCAL:
        return True
    # whole local without dots
    base = re.sub(r"[^a-z0-9]", "", loc)
    return base in GENERIC_LOCAL


def email_status_class(email_status: Optional[str], smtp_status: Optional[str]) -> str:
    est = (email_status or "").strip().lower()
    smtp = (smtp_status or "").strip().lower()
    if est in EMAIL_INVALID_STATUS or smtp in EMAIL_INVALID_STATUS:
        return "INVALIDO"
    if est in ("inferido", "inferred", "guessed"):
        return "INFERIDO"
    if est in EMAIL_VALID_STATUS or smtp in EMAIL_VALID_STATUS:
        return "VALIDADO"
    if est in ("accept_all", "catch_all", "catchall"):
        return "NAO_VALIDADO"  # accept-all não basta sozinho
    if est:
        return "NAO_VALIDADO"
    if smtp:
        return "NAO_VALIDADO"
    return "NAO_VALIDADO"


def email_nominal_match(nome: str, email: str) -> bool:
    """E-mail deve ser nominalmente do decisor (não de outra pessoa)."""
    local, _ = email_parts(email)
    if not local or not nome:
        return False
    # tokens do nome (>=3 chars)
    name_tokens = [
        norm_token(t)
        for t in re.split(r"\s+", nome.strip())
        if len(norm_token(t)) >= 3
    ]
    # drop common particles
    stop = {"dos", "das", "des", "del", "da", "de", "do", "e", "van", "von"}
    name_tokens = [t for t in name_tokens if t not in stop]
    if not name_tokens:
        return False
    local_n = norm_token(local.replace(".", " ").replace("_", " ").replace("-", " "))
    local_parts = [
        norm_token(p)
        for p in re.split(r"[.\-_]+", local)
        if len(norm_token(p)) >= 2
    ]
    # first name should appear in local (common patterns: nome.sobrenome, n.sobrenome, nomesobrenome)
    first = name_tokens[0]
    last = name_tokens[-1] if len(name_tokens) > 1 else ""
    # initial+lastname: fbarcellos
    initial_last = (first[0] + last) if last else ""
    checks = []
    if first and first in local_n:
        checks.append("first")
    if last and last in local_n:
        checks.append("last")
    if initial_last and initial_last in local_n and len(last) >= 4:
        checks.append("initial_last")
    # local parts overlap name tokens
    overlap = sum(1 for p in local_parts if any(p in t or t in p for t in name_tokens if len(p) >= 3))
    if overlap >= 2:
        checks.append("multi_token")
    if overlap == 1 and last and last in local_n:
        checks.append("one_plus_last")
    # require at least first OR (last + initial pattern) with evidence of name link
    if "first" in checks and (not last or "last" in checks or "initial_last" in checks or "multi_token" in checks):
        return True
    if "initial_last" in checks:
        return True
    if "multi_token" in checks and ("first" in checks or "last" in checks):
        return True
    # single token name
    if len(name_tokens) == 1 and first in local_n and len(first) >= 4:
        return True
    return False


def linkedin_ok(url: Optional[str]) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    if "linkedin.com" not in u:
        return False
    # perfil pessoal, não company
    if "/company/" in u or "/school/" in u:
        return False
    return "/in/" in u or "/pub/" in u


def cargo_ok(cargo: Optional[str]) -> bool:
    c = (cargo or "").strip()
    if len(c) < 2:
        return False
    # reject pure placeholders
    bad = {"n/a", "na", "nao informado", "não informado", "-", "sem cargo", "null", "none"}
    return c.lower() not in bad


def nome_ok(nome: Optional[str]) -> bool:
    n = (nome or "").strip()
    if len(n) < 3:
        return False
    if n.lower() in {"decisor", "contato", "empresa", "n/a", "nao informado"}:
        return False
    # at least one letter
    return bool(re.search(r"[A-Za-zÀ-ÿ]", n))


def capex_ok(valor: Any) -> bool:
    try:
        v = float(valor) if valor is not None else 0.0
    except (TypeError, ValueError):
        return False
    return v > 0


def evaluate_decisor(d: Dict[str, Any]) -> Dict[str, Any]:
    nome = d.get("nome") or ""
    cargo = d.get("cargo") or ""
    email = (d.get("email") or "").strip()
    li = d.get("linkedin_url") or ""
    tel = d.get("telefone") or ""
    est = d.get("email_status")
    smtp = d.get("email_smtp_status")
    hip = (d.get("hipotese_replicacao") or "")
    repl = hip == "REPLICADO_PROVAVEL_FALSO_POSITIVO"

    local, domain = email_parts(email)
    st_class = email_status_class(est, smtp) if email else "NAO_VALIDADO"
    gen = is_generic_local(local) if email else False
    personal = is_personal_domain(domain) if domain else False
    nominal = email_nominal_match(nome, email) if email and not gen else False

    if gen:
        st_class = "GENERICO"
    elif personal and email:
        st_class = "PESSOAL"
    elif email and st_class == "VALIDADO" and not nominal:
        # validado mas de outra pessoa / não nominal
        st_class = "DOMINIO_INCOMPATIVEL" if not nominal else st_class
        # keep as not qualifying nominal
        pass

    email_nominal_validado = bool(
        email
        and "@" in email
        and not gen
        and not personal
        and not repl
        and st_class == "VALIDADO"
        and nominal
    )
    # If status says validado but we reclassified to DOMINIO_INCOMPATIVEL due to non-nominal:
    if email and not gen and not personal and not repl and email_status_class(est, smtp) == "VALIDADO" and not nominal:
        email_nominal_validado = False
        st_class = "NAO_VALIDADO"  # nominal mismatch

    return {
        "nome_ok": nome_ok(nome) and not repl,
        "cargo_ok": cargo_ok(cargo),
        "linkedin_ok": linkedin_ok(li),
        "email": email or None,
        "email_status_class": st_class,
        "email_nominal_validado": email_nominal_validado,
        "domain": domain or None,
        "domain_corporate": bool(domain and not personal and not gen),
        "telefone": digits(tel) or None,
        "nome": nome,
        "cargo": cargo,
        "linkedin": li,
        "replicated": repl,
    }


def calc_tier(obra: Dict[str, Any], decisores: List[Dict[str, Any]]) -> Dict[str, Any]:
    cnpj_ok = cnpj_valido(obra.get("cnpj"))
    capex = capex_ok(obra.get("valor_estimado"))
    empresa = bool((obra.get("empresa") or "").strip() or cnpj_ok)

    best = None
    for d in decisores:
        ev = evaluate_decisor(d)
        if best is None:
            best = ev
        # prefer one that maximizes ouro readiness
        score = sum([
            ev["nome_ok"], ev["cargo_ok"], ev["linkedin_ok"],
            ev["email_nominal_validado"], ev["domain_corporate"],
        ])
        best_score = sum([
            best["nome_ok"], best["cargo_ok"], best["linkedin_ok"],
            best["email_nominal_validado"], best["domain_corporate"],
        ])
        if score > best_score or (score == best_score and ev["email_nominal_validado"] and not best["email_nominal_validado"]):
            best = ev

    if best is None:
        best = {
            "nome_ok": False, "cargo_ok": False, "linkedin_ok": False,
            "email_nominal_validado": False, "domain_corporate": False,
            "domain": None, "email": None, "email_status_class": None,
            "telefone": None, "nome": None, "cargo": None, "linkedin": None,
        }

    # domain criterion: domain from validated corporate email of decisor
    dominio_ok = bool(best.get("domain_corporate") and best.get("email_nominal_validado") and best.get("domain"))
    # also allow domain from validated corporate email even if we already require email - same
    if best.get("email_nominal_validado") and best.get("domain") and not is_personal_domain(best["domain"]):
        dominio_ok = True

    criterios = {
        "status_aprovada": True,  # universe filter
        "cnpj_valido": cnpj_ok,
        "dominio_corporativo": dominio_ok,
        "capex_valido": capex,
        "decisor_nome": best["nome_ok"],
        "decisor_cargo": best["cargo_ok"],
        "linkedin_confirmado": best["linkedin_ok"],
        "email_nominal_validado": best["email_nominal_validado"],
    }
    atendidos = [k for k, v in criterios.items() if v]
    ausentes = [k for k, v in criterios.items() if not v]

    all_ouro = all(criterios.values())
    if all_ouro:
        tier = "OURO"
        motivo = "Oito critérios obrigatórios atendidos (e-mail nominal validado; telefone é bônus)"
    elif empresa and best["nome_ok"] and best["cargo_ok"] and best["linkedin_ok"]:
        tier = "PRATA"
        motivo = "Decisor+cargo+LinkedIn sem e-mail nominal validado completo"
    elif empresa:
        tier = "BRONZE"
        motivo = "Empresa/CNPJ identificado; decisor comercial incompleto"
    else:
        tier = "PIPELINE"
        motivo = "Empresa/CNPJ insuficiente para classificação comercial"

    return {
        "tier": tier,
        "motivo": motivo,
        "criterios": criterios,
        "atendidos": atendidos,
        "ausentes": ausentes,
        "best": best,
        "cnpj_ok": cnpj_ok,
        "capex_ok": capex,
    }


def main():
    if not DRY_RUN and not APPLY:
        print("Use --dry-run ou --apply")
        sys.exit(2)

    conn = connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # snapshot if apply
    if APPLY and not DRY_RUN:
        cur.execute(
            """
            INSERT INTO wins_v2.tier_ouro_regra_final_snapshot
              (obra_id, classificacao_anterior, status_portao, valor_estimado, cnpj)
            SELECT id, classificacao_computed, status_portao, valor_estimado, cnpj
            FROM public.obras
            WHERE status_portao = 'APROVADA'
            ON CONFLICT (obra_id) DO UPDATE SET
              classificacao_anterior = EXCLUDED.classificacao_anterior,
              status_portao = EXCLUDED.status_portao,
              valor_estimado = EXCLUDED.valor_estimado,
              cnpj = EXCLUDED.cnpj,
              snapshot_em = now()
            """
        )
        conn.commit()
        print("snapshot ok", cur.rowcount)

    cur.execute(
        """
        SELECT id, nome, classificacao_computed, status_portao, valor_estimado, empresa, cnpj, cnpj_status
        FROM public.obras
        WHERE status_portao = 'APROVADA'
        ORDER BY id
        """
    )
    obras = cur.fetchall()
    print("APROVADAS", len(obras))

    cur.execute(
        """
        SELECT obra_id, nome, cargo, email, email_status, email_smtp_status,
               linkedin_url, telefone, telefone_fonte, confianca_match,
               hipotese_replicacao, qualidade_lead
        FROM public.decisores_obra
        WHERE excluido_em IS NULL
        """
    )
    by_obra: Dict[Any, List] = {}
    for d in cur.fetchall():
        by_obra.setdefault(d["obra_id"], []).append(d)

    stats = {
        "ouro_antes": 0,
        "ouro_final": 0,
        "prata_final": 0,
        "bronze_final": 0,
        "pipeline_final": 0,
        "mantidas_ouro": 0,
        "promovidas_prata_ouro": 0,
        "rebaixadas_ouro": 0,
        "ouro_sem_tel": 0,
        "ouro_com_tel": 0,
        "changed": 0,
    }
    changes = []
    pilots = {}

    for o in obras:
        if o["classificacao_computed"] == "OURO":
            stats["ouro_antes"] += 1
        decs = by_obra.get(o["id"], [])
        res = calc_tier(o, decs)
        tier = res["tier"]
        prev = o["classificacao_computed"]
        best = res["best"]

        if tier == "OURO":
            stats["ouro_final"] += 1
            if best.get("telefone"):
                stats["ouro_com_tel"] += 1
            else:
                stats["ouro_sem_tel"] += 1
            if prev == "OURO":
                stats["mantidas_ouro"] += 1
            elif prev == "PRATA":
                stats["promovidas_prata_ouro"] += 1
        elif tier == "PRATA":
            stats["prata_final"] += 1
            if prev == "OURO":
                stats["rebaixadas_ouro"] += 1
        elif tier == "BRONZE":
            stats["bronze_final"] += 1
            if prev == "OURO":
                stats["rebaixadas_ouro"] += 1
        else:
            stats["pipeline_final"] += 1
            if prev == "OURO":
                stats["rebaixadas_ouro"] += 1

        oid = str(o["id"])
        if oid in (
            "a7a6b4f4-19dd-4f7e-8647-f2123b7447e4",
            "b72d3db9-875b-4ec4-8678-4f574acecb93",
        ):
            pilots[oid] = {
                "prev": prev,
                "tier": tier,
                "motivo": res["motivo"],
                "criterios": res["criterios"],
                "best": {k: best.get(k) for k in (
                    "nome", "cargo", "email", "email_status_class",
                    "email_nominal_validado", "domain", "linkedin", "telefone",
                    "nome_ok", "cargo_ok", "linkedin_ok",
                )},
            }

        if prev != tier:
            stats["changed"] += 1
            changes.append((o["id"], prev, tier, res, best, o))

    print("STATS", json.dumps(stats, ensure_ascii=False, indent=2))
    print("PILOTS", json.dumps(pilots, ensure_ascii=False, indent=2, default=str))

    # quality gates on calculated OURO
    ouro_bad = {"no_email": 0, "no_cnpj": 0, "no_dom": 0, "no_capex": 0, "no_li": 0}
    for o in obras:
        decs = by_obra.get(o["id"], [])
        res = calc_tier(o, decs)
        if res["tier"] != "OURO":
            continue
        c = res["criterios"]
        if not c["email_nominal_validado"]:
            ouro_bad["no_email"] += 1
        if not c["cnpj_valido"]:
            ouro_bad["no_cnpj"] += 1
        if not c["dominio_corporativo"]:
            ouro_bad["no_dom"] += 1
        if not c["capex_valido"]:
            ouro_bad["no_capex"] += 1
        if not c["linkedin_confirmado"]:
            ouro_bad["no_li"] += 1
    print("OURO_BAD", ouro_bad)

    if DRY_RUN and not APPLY:
        print("dry-run only, no writes")
        conn.close()
        return stats, pilots, ouro_bad

    if APPLY:
        # batch update + audit
        audit_rows = []
        for obra_id, prev, tier, res, best, o in changes:
            cur.execute(
                "UPDATE public.obras SET classificacao_computed=%s WHERE id=%s AND status_portao='APROVADA'",
                (tier, obra_id),
            )
            audit_rows.append((
                obra_id, prev, tier,
                json.dumps(res["atendidos"]),
                json.dumps(res["ausentes"]),
                o.get("cnpj"),
                best.get("domain"),
                float(o["valor_estimado"]) if o.get("valor_estimado") is not None else None,
                best.get("nome"),
                best.get("cargo"),
                best.get("linkedin"),
                best.get("email"),
                best.get("email_status_class"),
                best.get("telefone"),
                None,  # tipo_telefone not used for tier
                res["motivo"],
                REGRA,
                json.dumps(res["criterios"]),
            ))
        # also audit OURO maintained (unchanged) for full traceability of final OURO set
        for o in obras:
            decs = by_obra.get(o["id"], [])
            res = calc_tier(o, decs)
            if res["tier"] == "OURO" and o["classificacao_computed"] == "OURO" and o["id"] not in {c[0] for c in changes}:
                best = res["best"]
                audit_rows.append((
                    o["id"], "OURO", "OURO",
                    json.dumps(res["atendidos"]),
                    json.dumps(res["ausentes"]),
                    o.get("cnpj"),
                    best.get("domain"),
                    float(o["valor_estimado"]) if o.get("valor_estimado") is not None else None,
                    best.get("nome"),
                    best.get("cargo"),
                    best.get("linkedin"),
                    best.get("email"),
                    best.get("email_status_class"),
                    best.get("telefone"),
                    None,
                    "Mantida OURO — 8 critérios OK",
                    REGRA,
                    json.dumps(res["criterios"]),
                ))

        execute_batch(cur, """
            INSERT INTO wins_v2.tier_ouro_regra_final_audit (
              obra_id, tier_anterior, tier_novo, criterios_atendidos, criterios_ausentes,
              cnpj, dominio, capex, decisor, cargo, linkedin, email, email_status,
              telefone, tipo_telefone, motivo, regra_aplicada, evidencias
            ) VALUES (
              %s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
            )
        """, audit_rows, page_size=500)
        conn.commit()
        print("applied changes", len(changes), "audit rows", len(audit_rows))

    conn.close()
    return stats, pilots, ouro_bad


if __name__ == "__main__":
    main()
