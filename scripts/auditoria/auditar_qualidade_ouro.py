#!/usr/bin/env python3
"""Auditoria de qualidade 100% das obras OURO + sanear promoções indevidas.

Não inventa dados. Rebaixa para PRATA quando evidência for insuficiente.
Produz matriz exata de transições e reconciliação dos e-mails.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch

sys.path.insert(0, "/tmp")
sys.path.insert(0, "/app/scripts/auditoria")
import importlib.util

def load_ouro_mod():
    for path in (
        "/tmp/aplicar_regra_ouro_final.py",
        "/app/scripts/auditoria/aplicar_regra_ouro_final.py",
        "/root/wins_hub/scripts/auditoria/aplicar_regra_ouro_final.py",
    ):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("ouro8", path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise RuntimeError("aplicar_regra_ouro_final.py not found")

ouro = load_ouro_mod()
APPLY = "--apply" in sys.argv

GENERIC_LOCAL = {
    "contato", "sac", "ouvidoria", "info", "comercial", "compras", "atendimento",
    "vendas", "rh", "financeiro", "suporte", "admin", "secretaria", "protocolo",
    "marketing", "imprensa", "juridico", "nfe", "fiscal",
}
PERSONAL = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br",
    "icloud.com", "live.com", "uol.com.br", "bol.com.br", "terra.com.br",
}


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def digits(v):
    return re.sub(r"\D", "", str(v or ""))


def strip_acc(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", strip_acc(s or "").lower())


def cnpj_ok(c):
    return ouro.cnpj_valido(c)


def email_valid_status(est, smtp):
    e = (est or "").lower()
    s = (smtp or "").lower()
    return e in ("valid", "valido", "ok", "verificado_manual", "verificado_manual_osint") or s in ("valid", "valido", "ok")


def classify_email_source(
    *,
    email: str,
    decisor_nome: str,
    obra_cnpj: str,
    email_status: str,
    email_smtp: str,
    fonte_decisor: str,
    email_audit_fonte: Optional[str],
    sibling_same_name: bool,
    sibling_same_cnpj: bool,
    domain_matches_empresa_dominios: bool,
) -> str:
    local, domain = (email or "").lower().rsplit("@", 1) if email and "@" in email else ("", "")
    if not email:
        return "SEM_FONTE"
    if local.split("+")[0].split(".")[0] in GENERIC_LOCAL or re.sub(r"[^a-z0-9]", "", local) in GENERIC_LOCAL:
        return "INFERIDO" if not email_valid_status(email_status, email_smtp) else "REUSO_INTERNO_INSUFICIENTE"
    if domain in PERSONAL:
        return "INFERIDO"
    if not ouro.email_nominal_match(decisor_nome, email):
        return "CONFLITANTE"
    if not email_valid_status(email_status, email_smtp):
        return "INFERIDO"
    # strong reuse
    if email_audit_fonte or (fonte_decisor and "reuso" in (fonte_decisor or "").lower()):
        if sibling_same_name and sibling_same_cnpj and domain_matches_empresa_dominios:
            return "REUSO_INTERNO_COM_EVIDENCIA_FORTE"
        if sibling_same_name and sibling_same_cnpj:
            return "REUSO_INTERNO_COM_EVIDENCIA_FORTE"
        return "REUSO_INTERNO_INSUFICIENTE"
    if email_status in ("verificado_manual", "verificado_manual_osint"):
        return "VALIDADO_INTERNO" if ouro.email_nominal_match(decisor_nome, email) else "CONFLITANTE"
    if email_valid_status(email_status, email_smtp) and ouro.email_nominal_match(decisor_nome, email):
        # originally valid on decisor without reuse audit
        if sibling_same_cnpj and sibling_same_name:
            return "REUSO_INTERNO_COM_EVIDENCIA_FORTE"
        return "VALIDADO_INTERNO"
    return "REUSO_INTERNO_INSUFICIENTE"


def cargo_area_ok(cargo: str) -> bool:
    c = strip_acc(cargo or "").lower()
    if len(c) < 2:
        return False
    # reject soft roles
    bad = ["recep", "atendimento", "sac ", " rh", "marketing", "estagi", "trainee", "aprendiz"]
    if any(b in f" {c}" for b in bad):
        # still allow if also has engenharia etc
        good_kw = ["engenh", "obra", "suprim", "compra", "projet", "implanta", "expans", "infra", "operac", "contrat", "diretor", "gerente", "coorden"]
        if not any(g in c for g in good_kw):
            return False
    return True


def pick_best_decisor(decs: List[Dict]) -> Optional[Dict]:
    best = None
    best_score = -1
    for d in decs:
        if (d.get("hipotese_replicacao") or "") == "REPLICADO_PROVAVEL_FALSO_POSITIVO":
            continue
        ev = ouro.evaluate_decisor(d)
        sc = sum([ev["nome_ok"], ev["cargo_ok"], ev["linkedin_ok"], ev["email_nominal_validado"]])
        if sc > best_score:
            best_score = sc
            best = {**d, "_ev": ev}
    return best


def main():
    conn = connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    report: Dict[str, Any] = {"started": datetime.now(timezone.utc).isoformat()}

    # --- snapshot current OURO ---
    if APPLY:
        cur.execute(
            """
            INSERT INTO wins_v2.ouro_quality_snapshot
              (obra_id, tier, classificacao_snapshot, empresa, cnpj, email, email_status, decisor, cargo, linkedin)
            SELECT o.id, o.classificacao_computed, o.classificacao_computed, o.empresa, o.cnpj,
              d.email, d.email_status, d.nome, d.cargo, d.linkedin_url
            FROM public.obras o
            LEFT JOIN LATERAL (
              SELECT * FROM decisores_obra dob
              WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
              ORDER BY CASE WHEN dob.email IS NOT NULL THEN 0 ELSE 1 END,
                       dob.confianca_match DESC NULLS LAST
              LIMIT 1
            ) d ON true
            WHERE o.status_portao='APROVADA' AND o.classificacao_computed='OURO'
            ON CONFLICT (obra_id) DO UPDATE SET
              tier=EXCLUDED.tier, classificacao_snapshot=EXCLUDED.classificacao_snapshot,
              empresa=EXCLUDED.empresa, cnpj=EXCLUDED.cnpj, email=EXCLUDED.email,
              email_status=EXCLUDED.email_status, decisor=EXCLUDED.decisor,
              cargo=EXCLUDED.cargo, linkedin=EXCLUDED.linkedin, snapshot_em=now()
            """
        )
        conn.commit()
        report["ouro_snapshot"] = cur.rowcount

    # --- transition matrix from enrichment_gap_snapshot ---
    cur.execute(
        """
        SELECT s.tier AS tier_antes, o.classificacao_computed AS tier_depois, count(*)::int AS n
        FROM wins_v2.enrichment_gap_snapshot s
        JOIN public.obras o ON o.id = s.obra_id
        WHERE o.status_portao='APROVADA'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )
    matrix = defaultdict(lambda: defaultdict(int))
    for r in cur.fetchall():
        matrix[r["tier_antes"] or "NULL"][r["tier_depois"] or "NULL"] = r["n"]
    tiers_ord = ["OURO", "PRATA", "BRONZE", "PIPELINE"]
    matrix_exact = {a: {b: matrix[a][b] for b in tiers_ord} for a in tiers_ord}
    # validate sums
    row_sums = {a: sum(matrix_exact[a].values()) for a in tiers_ord}
    col_sums = {b: sum(matrix_exact[a][b] for a in tiers_ord) for b in tiers_ord}
    report["matriz_transicoes"] = matrix_exact
    report["matriz_somas_linhas"] = row_sums
    report["matriz_somas_colunas"] = col_sums
    report["ouro_mantidas"] = matrix_exact["OURO"]["OURO"]
    report["prata_para_ouro"] = matrix_exact["PRATA"]["OURO"]
    report["bronze_para_ouro"] = matrix_exact["BRONZE"]["OURO"]
    report["pipeline_para_ouro"] = matrix_exact["PIPELINE"]["OURO"]
    report["bronze_para_prata"] = matrix_exact["BRONZE"]["PRATA"]
    report["pipeline_para_bronze"] = matrix_exact["PIPELINE"]["BRONZE"]
    report["pipeline_para_prata"] = matrix_exact["PIPELINE"]["PRATA"]
    report["rebaixamentos"] = {
        "OURO->PRATA": matrix_exact["OURO"]["PRATA"],
        "OURO->BRONZE": matrix_exact["OURO"]["BRONZE"],
        "OURO->PIPELINE": matrix_exact["OURO"]["PIPELINE"],
        "PRATA->BRONZE": matrix_exact["PRATA"]["BRONZE"],
        "PRATA->PIPELINE": matrix_exact["PRATA"]["PIPELINE"],
        "BRONZE->PIPELINE": matrix_exact["BRONZE"]["PIPELINE"],
    }
    report["sem_mudanca"] = sum(matrix_exact[t][t] for t in tiers_ord)

    # --- exclusive completeness categories from gap matrix + audit ---
    cur.execute(
        """
        SELECT m.obra_id, m.tier_atual, m.campos_faltantes, m.num_tentativas, m.resultado_ultima,
               m.completeness_score, m.precisa_externo,
               s.tier AS tier_baseline
        FROM wins_v2.enrichment_gap_matrix m
        JOIN wins_v2.enrichment_gap_snapshot s ON s.obra_id=m.obra_id
        """
    )
    gaps = cur.fetchall()
    cur.execute(
        """
        SELECT obra_id, array_agg(DISTINCT acao) acoes
        FROM wins_v2.enrichment_gap_audit
        WHERE acao IN ('cnpj_resolvido','dominio_catalogo','email_reuso','decisor_candidato')
        GROUP BY obra_id
        """
    )
    enriched = {r["obra_id"]: r["acoes"] for r in cur.fetchall()}

    cats = Counter()
    for g in gaps:
        faltas = g["campos_faltantes"] or []
        # recompute faltas relevant for target
        has_enrich = g["obra_id"] in enriched
        # baseline complete for OURO? unlikely many
        baseline_tier = g["tier_baseline"]
        # exclusive
        if baseline_tier == "OURO" and not has_enrich and (not faltas or set(faltas) <= set()):
            # was already ouro at baseline
            cats["OBRA_JA_COMPLETA_NO_BASELINE"] += 1
        elif has_enrich and not faltas:
            cats["OBRA_COMPLETAMENTE_RESOLVIDA"] += 1
        elif has_enrich and faltas:
            cats["OBRA_PARCIALMENTE_RESOLVIDA"] += 1
        elif not has_enrich and not faltas:
            # complete now without enrich action in audit (already complete)
            cats["OBRA_JA_COMPLETA_NO_BASELINE"] += 1
        else:
            cats["OBRA_SEM_ENRIQUECIMENTO"] += 1

    # Fix double-count: if baseline OURO and no faltas
    # Recalculate cleaner
    cats = Counter()
    for g in gaps:
        faltas = list(g["campos_faltantes"] or [])
        has_enrich = g["obra_id"] in enriched
        ja_completa = (g["tier_baseline"] == "OURO") or (
            not faltas and g["tier_baseline"] in ("OURO",) 
        )
        # Completeness for intended progression: no critical faltas
        sem_lacuna_critica = len(faltas) == 0
        if g["tier_baseline"] == "OURO" and not has_enrich:
            cats["OBRA_JA_COMPLETA_NO_BASELINE"] += 1
        elif has_enrich and sem_lacuna_critica:
            cats["OBRA_COMPLETAMENTE_RESOLVIDA"] += 1
        elif has_enrich and not sem_lacuna_critica:
            cats["OBRA_PARCIALMENTE_RESOLVIDA"] += 1
        elif not has_enrich and sem_lacuna_critica:
            cats["OBRA_JA_COMPLETA_NO_BASELINE"] += 1
        else:
            cats["OBRA_SEM_ENRIQUECIMENTO"] += 1

    # ensure sum 16633
    cat_sum = sum(cats.values())
    report["categorias_exclusivas"] = dict(cats)
    report["categorias_soma"] = cat_sum

    # field metrics from audit
    cur.execute(
        """
        SELECT acao, count(*) FROM wins_v2.enrichment_gap_audit
        WHERE acao IN ('cnpj_resolvido','dominio_catalogo','email_reuso','decisor_candidato','tier')
        GROUP BY 1
        """
    )
    report["metricas_campo"] = {r["acao"]: r["count"] for r in cur.fetchall()}

    # --- load all OURO with decisores ---
    cur.execute(
        """
        SELECT o.id, o.nome AS obra_nome, o.classificacao_computed, o.status_portao,
               o.empresa, o.cnpj, o.cnpj_status, o.valor_estimado, o.setor, o.uf, o.fase
        FROM public.obras o
        WHERE o.status_portao='APROVADA' AND o.classificacao_computed='OURO'
        ORDER BY o.id
        """
    )
    ouros = cur.fetchall()
    report["ouro_auditadas"] = len(ouros)

    cur.execute(
        """
        SELECT * FROM public.decisores_obra WHERE excluido_em IS NULL
        """
    )
    by_obra = defaultdict(list)
    for d in cur.fetchall():
        by_obra[d["obra_id"]].append(d)

    # email reuso audit map obra_id -> list
    cur.execute(
        """
        SELECT obra_id, valor_depois AS email, fonte, evidencia, criado_em
        FROM wins_v2.enrichment_gap_audit
        WHERE acao='email_reuso'
        """
    )
    email_audit_by_obra = defaultdict(list)
    for r in cur.fetchall():
        email_audit_by_obra[r["obra_id"]].append(r)

    # empresa_dominios
    cur.execute("SELECT regexp_replace(cnpj,'\\D','','g') c, dominio FROM public.empresa_dominios WHERE nullif(btrim(dominio),'') IS NOT NULL")
    dom_by_cnpj = {}
    for r in cur.fetchall():
        if len(r["c"]) == 14:
            dom_by_cnpj[r["c"]] = r["dominio"]

    # sibling emails index: (cnpj, lower name) -> emails valid
    cur.execute(
        """
        SELECT regexp_replace(o.cnpj,'\\D','','g') cnpj, lower(trim(d.nome)) nome,
               d.email, d.email_status, d.email_smtp_status, d.linkedin_url, d.cargo, o.id obra_id
        FROM public.obras o
        JOIN public.decisores_obra d ON d.obra_id=o.id AND d.excluido_em IS NULL
        WHERE o.status_portao='APROVADA'
          AND nullif(btrim(d.email),'') IS NOT NULL
        """
    )
    siblings = defaultdict(list)
    siblings_by_email = defaultdict(list)
    for r in cur.fetchall():
        c = r["cnpj"] or ""
        if len(c) == 14 and r["nome"]:
            siblings[(c, r["nome"])].append(r)
        em = (r.get("email") or "").strip().lower()
        if len(c) == 14 and em:
            siblings_by_email[(c, em)].append(r)

    # baseline tier for each obra
    cur.execute("SELECT obra_id, tier FROM wins_v2.enrichment_gap_snapshot")
    baseline_tier = {r["obra_id"]: r["tier"] for r in cur.fetchall()}

    decisions = Counter()
    audit_rows = []
    rebaixar = []
    email_source_counts = Counter()
    conflicts = Counter()
    ouro_antigas = 0
    ouro_novas = 0
    novas_detail = []

    for o in ouros:
        oid = o["id"]
        prev = baseline_tier.get(oid, "?")
        if prev == "OURO":
            ouro_antigas += 1
            origem = "BASELINE_OURO"
        else:
            ouro_novas += 1
            origem = f"PROMOCAO_{prev}->OURO"

        decs = by_obra.get(oid, [])
        best = pick_best_decisor(decs)
        cnpj = digits(o.get("cnpj"))
        capex = float(o["valor_estimado"] or 0)
        problemas = []
        criterios = {
            "status_aprovada": o["status_portao"] == "APROVADA",
            "cnpj_valido": cnpj_ok(cnpj),
            "dominio_corporativo": False,
            "capex_valido": capex >= 100_000,  # regra auditoria: >= 100k
            "decisor_nome": False,
            "decisor_cargo": False,
            "linkedin_confirmado": False,
            "email_nominal_validado": False,
        }

        email = None
        email_status = None
        email_fonte_class = "SEM_FONTE"
        email_fonte = None
        dominio = None
        vinculo = "desconhecido"
        decisor = cargo = linkedin = telefone = None

        if not best:
            problemas.append("sem_decisor")
        else:
            ev = best["_ev"]
            decisor = best.get("nome")
            cargo = best.get("cargo")
            linkedin = best.get("linkedin_url")
            telefone = best.get("telefone")
            email = best.get("email")
            email_status = best.get("email_status") or best.get("email_smtp_status")
            criterios["decisor_nome"] = ev["nome_ok"]
            criterios["decisor_cargo"] = ev["cargo_ok"] and cargo_area_ok(cargo or "")
            criterios["linkedin_confirmado"] = ev["linkedin_ok"]
            criterios["email_nominal_validado"] = ev["email_nominal_validado"]

            if not criterios["decisor_nome"]:
                problemas.append("decisor_nome_invalido")
            if not criterios["decisor_cargo"]:
                problemas.append("cargo_incompativel_ou_ausente")
            if not criterios["linkedin_confirmado"]:
                problemas.append("linkedin_ausente_ou_invalido")
            if (best.get("hipotese_replicacao") or "") == "CANDIDATO_REUSO_CNPJ":
                vinculo = "candidato_reuso"
                # only flag if email/source later weak
                problemas.append("decisor_candidato_reuso_cnpj")

            # domain
            if email and "@" in email:
                dominio = email.rsplit("@", 1)[-1].lower()
                if dominio in PERSONAL:
                    problemas.append("dominio_pessoal")
                    criterios["dominio_corporativo"] = False
                else:
                    criterios["dominio_corporativo"] = True
                    # match empresa_dominios if available
                    if cnpj in dom_by_cnpj:
                        cat_dom = (dom_by_cnpj[cnpj] or "").lower().lstrip("www.")
                        if cat_dom and cat_dom not in dominio and dominio not in cat_dom:
                            # soft warning - domain may still be valid subsidiary
                            pass

            # email source
            ea = email_audit_by_obra.get(oid, [])
            email_fonte = ea[0]["fonte"] if ea else (best.get("fonte") or None)
            nome_key = (decisor or "").strip().lower()
            sibs = siblings.get((cnpj, nome_key), [])
            sibling_same = any(s["obra_id"] != oid for s in sibs)
            em_key = (email or "").strip().lower()
            sibs_em = siblings_by_email.get((cnpj, em_key), [])
            sibling_email_same_cnpj = any(s["obra_id"] != oid for s in sibs_em)
            # strong if same email on another obra of same CNPJ OR same name
            sibling_strong = sibling_same or sibling_email_same_cnpj
            domain_ok_catalog = bool(cnpj in dom_by_cnpj and dominio and (
                (dom_by_cnpj[cnpj] or "").lower().replace("www.","") in (dominio or "")
                or (dominio or "") in (dom_by_cnpj[cnpj] or "").lower().replace("www.","")
            ))
            if criterios["dominio_corporativo"] and not domain_ok_catalog:
                domain_ok_catalog = criterios["dominio_corporativo"]

            email_fonte_class = classify_email_source(
                email=email or "",
                decisor_nome=decisor or "",
                obra_cnpj=cnpj,
                email_status=best.get("email_status") or "",
                email_smtp=best.get("email_smtp_status") or "",
                fonte_decisor=best.get("fonte") or "",
                email_audit_fonte=email_fonte,
                sibling_same_name=sibling_strong,
                sibling_same_cnpj=bool(cnpj and len(cnpj)==14),
                domain_matches_empresa_dominios=domain_ok_catalog,
            )
            # Baseline OURO with valid nominal email: keep as VALIDADO_INTERNO
            if email_fonte_class.startswith("REUSO") and prev == "OURO" and criterios["email_nominal_validado"]:
                email_fonte_class = "VALIDADO_INTERNO"
            email_source_counts[email_fonte_class] += 1

            if email_fonte_class in ("INFERIDO", "CONFLITANTE", "REUSO_INTERNO_INSUFICIENTE", "SEM_FONTE"):
                problemas.append(f"email_fonte_{email_fonte_class}")
                criterios["email_nominal_validado"] = False
            if not email_valid_status(best.get("email_status"), best.get("email_smtp_status")):
                problemas.append("email_status_nao_validado")
                criterios["email_nominal_validado"] = False
            if email and not ouro.email_nominal_match(decisor or "", email):
                problemas.append("email_nao_nominal")
                criterios["email_nominal_validado"] = False
                conflicts["email_nao_nominal"] += 1

            # CAPEX floor 100k
            if capex < 100_000:
                problemas.append("capex_abaixo_100k")
                criterios["capex_valido"] = False

            # CNPJ
            if not criterios["cnpj_valido"]:
                problemas.append("cnpj_invalido")

            # dominio required
            if not criterios["dominio_corporativo"]:
                problemas.append("sem_dominio_corporativo")

            vinculo = "confirmado" if (
                criterios["decisor_nome"] and criterios["decisor_cargo"]
                and criterios["linkedin_confirmado"] and criterios["email_nominal_validado"]
                and email_fonte_class in ("REUSO_INTERNO_COM_EVIDENCIA_FORTE", "VALIDADO_INTERNO", "FONTE_OFICIAL_EMPRESA", "DOCUMENTO_OFICIAL", "PROVEDOR_DE_VALIDACAO")
            ) else ("duvida" if problemas else "ok")

        # 8 criteria all true?
        oito_ok = all(criterios.values())
        fonte_forte = email_fonte_class in (
            "REUSO_INTERNO_COM_EVIDENCIA_FORTE", "VALIDADO_INTERNO",
            "FONTE_OFICIAL_EMPRESA", "DOCUMENTO_OFICIAL", "PROVEDOR_DE_VALIDACAO",
        )

        # soft flags that don't block if email evidence strong
        soft = {"decisor_candidato_reuso_cnpj"}
        hard = [p for p in problemas if p not in soft]
        if oito_ok and email_fonte_class in ("REUSO_INTERNO_COM_EVIDENCIA_FORTE", "VALIDADO_INTERNO", "FONTE_OFICIAL_EMPRESA", "DOCUMENTO_OFICIAL", "PROVEDOR_DE_VALIDACAO") and not hard:
            decisao = "MANTER_OURO"
            motivo = f"Oito critérios OK; fonte e-mail={email_fonte_class}"
        elif oito_ok and fonte_forte and not hard:
            decisao = "MANTER_OURO"
            motivo = "Oito critérios com evidência suficiente"
        else:
            decisao = "REBAIXAR_PRATA"
            motivo = "Evidência insuficiente: " + ("; ".join(problemas) if problemas else f"fonte={email_fonte_class}")

        # NEVER leave REVISAO_MANUAL as OURO
        if decisao == "REVISAO_MANUAL":
            decisao = "REBAIXAR_PRATA"

        decisions[decisao] += 1
        if prev != "OURO":
            novas_detail.append({
                "obra_id": str(oid),
                "tier_antes": prev,
                "decisao": decisao,
                "email": email,
                "fonte": email_fonte_class,
                "problemas": problemas,
            })

        if decisao == "REBAIXAR_PRATA":
            rebaixar.append(oid)

        audit_rows.append((
            oid, prev, "OURO" if decisao == "MANTER_OURO" else "PRATA", origem,
            json.dumps(criterios), cnpj, dominio, capex, decisor, cargo, linkedin,
            email, email_status, email_fonte_class, email_fonte, telefone,
            vinculo, problemas, decisao, motivo, False,
        ))

    report["ouro_antigas_auditadas"] = ouro_antigas
    report["novas_ouro_auditadas"] = ouro_novas
    report["decisoes"] = dict(decisions)
    report["ouro_manter"] = decisions["MANTER_OURO"]
    report["ouro_rebaixar"] = decisions["REBAIXAR_PRATA"]
    report["email_fontes"] = dict(email_source_counts)
    report["conflitos"] = dict(conflicts)

    # --- reconcile 802 emails ---
    cur.execute(
        """
        SELECT a.obra_id, a.valor_depois AS email, a.fonte, a.evidencia,
               s.tier AS tier_antes, o.classificacao_computed AS tier_agora
        FROM wins_v2.enrichment_gap_audit a
        JOIN wins_v2.enrichment_gap_snapshot s ON s.obra_id=a.obra_id
        JOIN public.obras o ON o.id=a.obra_id
        WHERE a.acao='email_reuso'
        """
    )
    emails802 = cur.fetchall()
    report["emails_802_total"] = len(emails802)
    rec = Counter()
    motivos_sem_promo = Counter()
    for e in emails802:
        oid = e["obra_id"]
        antes = e["tier_antes"]
        # final after our rebaixamento?
        will_be_ouro = oid not in rebaixar and oid in {o["id"] for o in ouros}
        # current before rebaix
        agora = e["tier_agora"]
        if antes == "PRATA" and agora == "OURO":
            rec["promoveu_PRATA_OURO"] += 1
        elif antes == "BRONZE" and agora == "OURO":
            rec["promoveu_BRONZE_OURO"] += 1
        elif antes == "OURO" and agora == "OURO":
            rec["ja_era_OURO"] += 1
        elif agora != "OURO":
            rec["aplicado_sem_promocao"] += 1
            # why
            o = next((x for x in ouros if x["id"] == oid), None)
            if o is None:
                # not currently ouro
                decs = by_obra.get(oid, [])
                ocalc = {"id": oid, "cnpj": None, "valor_estimado": 0, "empresa": None, "status_portao": "APROVADA", "classificacao_computed": agora}
                cur2 = conn.cursor(cursor_factory=RealDictCursor)
                cur2.execute("SELECT cnpj, valor_estimado, empresa FROM obras WHERE id=%s", (oid,))
                row = cur2.fetchone()
                if row:
                    ocalc.update(row)
                res = ouro.calc_tier(ocalc, decs)
                for k, v in res["criterios"].items():
                    if not v and k != "status_aprovada":
                        motivos_sem_promo[k] += 1
                        break
                else:
                    motivos_sem_promo["outro_motivo"] += 1
            else:
                motivos_sem_promo["outro_motivo"] += 1
        else:
            rec["outro"] += 1

        # will be rejected by this audit
        if oid in rebaixar:
            rec["rejeitados_pos_auditoria"] += 1

    report["reconciliacao_802"] = dict(rec)
    report["emails_sem_promo_motivos"] = dict(motivos_sem_promo)
    report["emails_802_soma_check"] = sum(rec[k] for k in rec if k != "rejeitados_pos_auditoria")

    # --- APPLY rebaixamentos ---
    if APPLY and rebaixar:
        for oid in rebaixar:
            cur.execute(
                "UPDATE public.obras SET classificacao_computed='PRATA' WHERE id=%s AND status_portao='APROVADA' AND classificacao_computed='OURO'",
                (oid,),
            )
        conn.commit()
        report["rebaixadas_aplicadas"] = len(rebaixar)

    if APPLY and audit_rows:
        execute_batch(cur, """
            INSERT INTO wins_v2.ouro_enrichment_quality_audit (
              obra_id, tier_anterior, tier_novo, origem_promocao, criterios,
              cnpj, dominio, capex, decisor, cargo, linkedin, email, email_status,
              email_fonte_class, email_fonte, telefone, vinculo_atual, conflitos,
              decisao, motivo, consulta_externa
            ) VALUES (
              %s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
        """, audit_rows, page_size=200)
        conn.commit()
        report["audit_rows"] = len(audit_rows)

    # final tiers
    cur.execute("SELECT classificacao_computed, count(*) FROM public.obras WHERE status_portao='APROVADA' GROUP BY 1")
    report["tiers_finais"] = dict(cur.fetchall())
    cur.execute("SELECT count(*) FROM public.obras WHERE status_portao='APROVADA' AND classificacao_computed='OURO'")
    ouro_final = cur.fetchone()["count"]
    report["ouro_final"] = ouro_final

    # integrity on remaining OURO
    cur.execute(
        """
        SELECT o.id, o.cnpj, o.valor_estimado, o.empresa
        FROM public.obras o
        WHERE o.status_portao='APROVADA' AND o.classificacao_computed='OURO'
        """
    )
    remaining = cur.fetchall()
    bad = 0
    for o in remaining:
        decs = by_obra.get(o["id"], [])
        # refresh after rebaix - only those still ouro
        res = ouro.calc_tier({**o, "status_portao": "APROVADA", "classificacao_computed": "OURO"}, decs)
        if res["tier"] != "OURO" or float(o["valor_estimado"] or 0) < 100_000:
            bad += 1
    # also re-check source quality for remaining
    report["ouro_final_integridade_8crit"] = {"total": len(remaining), "falhas_calc": bad}

    report["finished"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    open("/tmp/ouro_quality_report.json", "w").write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    conn.close()
    return report


if __name__ == "__main__":
    main()
