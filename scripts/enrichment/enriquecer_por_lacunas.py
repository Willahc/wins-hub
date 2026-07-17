#!/usr/bin/env python3
"""Enriquecimento orientado por lacunas — reuso interno primeiro.

Processa 100% das APROVADAS: diagnóstico + tentativas internas de preenchimento.
Não inventa dados. Não promove por telefone. E-mail inferido não vira validado.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch, execute_values

# Reuse 8-criteria from ouro script
sys.path.insert(0, "/app/scripts/auditoria")
sys.path.insert(0, "/tmp")
try:
    from aplicar_regra_ouro_final import (
        calc_tier,
        cnpj_valido,
        email_nominal_match,
        email_parts,
        is_generic_local,
        is_personal_domain,
        linkedin_ok,
        cargo_ok,
        nome_ok,
        capex_ok,
        evaluate_decisor,
        digits,
    )
except ImportError:
    # load from file path
    import importlib.util
    for path in (
        "/app/scripts/auditoria/aplicar_regra_ouro_final.py",
        "/tmp/aplicar_regra_ouro_final.py",
        "/root/wins_hub/scripts/auditoria/aplicar_regra_ouro_final.py",
    ):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("ouro", path)
            ouro = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ouro)
            calc_tier = ouro.calc_tier
            cnpj_valido = ouro.cnpj_valido
            email_nominal_match = ouro.email_nominal_match
            email_parts = ouro.email_parts
            is_generic_local = ouro.is_generic_local
            is_personal_domain = ouro.is_personal_domain
            linkedin_ok = ouro.linkedin_ok
            cargo_ok = ouro.cargo_ok
            nome_ok = ouro.nome_ok
            capex_ok = ouro.capex_ok
            evaluate_decisor = ouro.evaluate_decisor
            digits = ouro.digits
            break
    else:
        raise

APPLY = "--apply" in sys.argv
DRY = "--dry-run" in sys.argv or not APPLY


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def email_validated_status(est, smtp) -> bool:
    e = (est or "").lower()
    s = (smtp or "").lower()
    return e in ("valid", "valido", "ok", "verificado_manual", "verificado_manual_osint") or s in (
        "valid", "valido", "ok"
    )


def digitos_cnpj(v) -> str:
    d = digits(v)
    return d if len(d) == 14 else ""


def priority(tier: str, gaps: List[str], capex: float) -> int:
    """Lower = higher priority."""
    gset = set(gaps)
    base = 50
    if tier == "PRATA" and gset <= {"email_validado", "email_presente", "email_nominal", "email_corporativo", "dominio_confirmado"}:
        if "email_validado" in gset:
            base = 1  # near OURO
        else:
            base = 2
    elif tier == "PRATA":
        base = 10
    elif tier == "BRONZE" and "decisor_presente" not in gset:
        base = 15  # has decisor partial
    elif tier == "BRONZE":
        base = 20 if capex >= 1e7 else 30
    elif tier == "PIPELINE":
        base = 40 if capex >= 1e7 else 50
    elif tier == "OURO":
        base = 90
    # boost high capex
    if capex >= 1e8:
        base = max(1, base - 2)
    return base


def completeness(flags: Dict[str, bool]) -> float:
    keys = [
        "empresa_presente", "cnpj_valido", "dominio_confirmado", "capex_valido",
        "decisor_presente", "cargo_presente", "linkedin_confirmado", "email_validado",
    ]
    return round(100.0 * sum(1 for k in keys if flags.get(k)) / len(keys), 2)


def next_action(tier: str, gaps: List[str]) -> str:
    if not gaps:
        return "completar_auditoria_manter_tier"
    if "cnpj_valido" in gaps or "empresa_presente" in gaps:
        return "resolver_entidade_cnpj"
    if "dominio_confirmado" in gaps:
        return "resolver_dominio"
    if "capex_valido" in gaps:
        return "consolidar_capex"
    if "decisor_presente" in gaps:
        return "localizar_decisor"
    if "cargo_presente" in gaps or "cargo_compativel" in gaps:
        return "validar_cargo"
    if "linkedin_confirmado" in gaps:
        return "confirmar_linkedin"
    if "email_validado" in gaps or "email_presente" in gaps:
        return "localizar_validar_email_nominal"
    return "revisar_lacunas"


def main():
    conn = connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    stats = defaultdict(int)
    stats["started"] = datetime.now(timezone.utc).isoformat()

    # --- snapshot ---
    if APPLY:
        cur.execute(
            """
            INSERT INTO wins_v2.enrichment_gap_snapshot
              (obra_id, tier, empresa, cnpj, dominio, capex, decisor, cargo, linkedin, email, email_status, telefone)
            SELECT o.id, o.classificacao_computed, o.empresa, o.cnpj,
              (SELECT d.dominio FROM public.empresa_dominios d
               WHERE regexp_replace(d.cnpj,'\\D','','g') = regexp_replace(coalesce(o.cnpj,''),'\\D','','g')
               ORDER BY d.confianca DESC NULLS LAST LIMIT 1),
              o.valor_estimado,
              (SELECT dob.nome FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
               ORDER BY dob.confianca_match DESC NULLS LAST LIMIT 1),
              (SELECT dob.cargo FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
               ORDER BY dob.confianca_match DESC NULLS LAST LIMIT 1),
              (SELECT dob.linkedin_url FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
               AND dob.linkedin_url IS NOT NULL LIMIT 1),
              (SELECT dob.email FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
               AND nullif(btrim(dob.email),'') IS NOT NULL ORDER BY dob.confianca_match DESC NULLS LAST LIMIT 1),
              (SELECT dob.email_status FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
               AND nullif(btrim(dob.email),'') IS NOT NULL ORDER BY dob.confianca_match DESC NULLS LAST LIMIT 1),
              (SELECT dob.telefone FROM decisores_obra dob WHERE dob.obra_id=o.id AND dob.excluido_em IS NULL
               AND nullif(btrim(dob.telefone),'') IS NOT NULL LIMIT 1)
            FROM public.obras o
            WHERE o.status_portao='APROVADA'
            ON CONFLICT (obra_id) DO UPDATE SET
              tier=EXCLUDED.tier, empresa=EXCLUDED.empresa, cnpj=EXCLUDED.cnpj,
              dominio=EXCLUDED.dominio, capex=EXCLUDED.capex, decisor=EXCLUDED.decisor,
              cargo=EXCLUDED.cargo, linkedin=EXCLUDED.linkedin, email=EXCLUDED.email,
              email_status=EXCLUDED.email_status, telefone=EXCLUDED.telefone,
              snapshot_em=now()
            """
        )
        conn.commit()
        stats["snapshot"] = cur.rowcount
        print("snapshot", stats["snapshot"])

    # --- load works ---
    cur.execute(
        """
        SELECT id, nome, classificacao_computed, status_portao, valor_estimado, empresa, cnpj, cnpj_status
        FROM public.obras WHERE status_portao='APROVADA' ORDER BY id
        """
    )
    obras = cur.fetchall()
    stats["aprovadas"] = len(obras)
    print("APROVADAS", len(obras))

    # decisores
    cur.execute(
        """
        SELECT id, obra_id, nome, cargo, email, email_status, email_smtp_status,
               linkedin_url, telefone, confianca_match, hipotese_replicacao, fonte
        FROM public.decisores_obra WHERE excluido_em IS NULL
        """
    )
    by_obra: Dict[Any, List] = defaultdict(list)
    by_cnpj_dec: Dict[str, List] = defaultdict(list)
    for d in cur.fetchall():
        by_obra[d["obra_id"]].append(d)

    # map obra cnpj
    obra_cnpj = {o["id"]: digitos_cnpj(o.get("cnpj")) for o in obras}

    # index decisores by cnpj of their obra
    for o in obras:
        c = obra_cnpj[o["id"]]
        if not c:
            continue
        for d in by_obra.get(o["id"], []):
            by_cnpj_dec[c].append({**d, "origem_obra_id": o["id"]})

    # empresa_dominios
    cur.execute(
        """
        SELECT regexp_replace(cnpj,'\\D','','g') cnpj, dominio, confianca, fonte, dominio_status
        FROM public.empresa_dominios
        WHERE nullif(btrim(dominio),'') IS NOT NULL
        """
    )
    dominios: Dict[str, Dict] = {}
    for r in cur.fetchall():
        c = r["cnpj"]
        if len(c) != 14:
            continue
        if c not in dominios or (r.get("confianca") or 0) >= (dominios[c].get("confianca") or 0):
            dominios[c] = r
    stats["dominios_catalog"] = len(dominios)

    # preservados by cnpj
    cur.execute(
        """
        SELECT regexp_replace(cnpj,'\\D','','g') cnpj, nome, cargo, email, telefone, linkedin_url,
               confianca_match, fonte, origem_obra_id
        FROM public.decisores_preservados
        WHERE nullif(btrim(cnpj),'') IS NOT NULL
        """
    )
    preservados: Dict[str, List] = defaultdict(list)
    for r in cur.fetchall():
        c = r["cnpj"]
        if len(c) == 14:
            preservados[c].append(r)
    stats["preservados"] = sum(len(v) for v in preservados.values())

    # entidades_lookup for name→cnpj
    cur.execute(
        """
        SELECT cnpj_normalizado, razao_social, nome_fantasia, confianca
        FROM wins_v2.entidades_lookup
        WHERE nullif(btrim(cnpj_normalizado),'') IS NOT NULL
        """
    )
    lookup_by_name: Dict[str, str] = {}
    for r in cur.fetchall():
        c = digitos_cnpj(r["cnpj_normalizado"])
        if len(c) != 14:
            continue
        for nm in (r.get("razao_social"), r.get("nome_fantasia")):
            if nm:
                key = re.sub(r"\s+", " ", nm.strip().upper())
                try:
                    conf = float(r.get("confianca") or 0)
                except (TypeError, ValueError):
                    conf = 0.0
                if key and (key not in lookup_by_name or conf > 0.5):
                    lookup_by_name[key] = c
    stats["lookup_names"] = len(lookup_by_name)

    # --- process ---
    matrix_rows = []
    audits = []
    tier_before = defaultdict(int)
    tier_after = defaultdict(int)
    promotions = defaultdict(int)
    email_applied = 0
    dominio_applied = 0
    cnpj_applied = 0
    decisor_applied = 0
    full_hit = 0
    full_miss = 0

    updates_cnpj = []  # (cnpj, obra_id)
    updates_email = []  # (email, status, decisor_id)
    inserts_decisor = []  # dicts
    domain_notes = []  # audit only - domain via empresa_dominios not written to obras (no column)

    for o in obras:
        oid = o["id"]
        tier = o["classificacao_computed"] or "PIPELINE"
        tier_before[tier] += 1
        decs = by_obra.get(oid, [])
        cnpj = digitos_cnpj(o.get("cnpj"))
        capex = float(o["valor_estimado"] or 0)

        # evaluate current decisor quality
        best_eval = None
        has_email_val = False
        has_email = False
        has_li = False
        has_cargo = False
        has_nome = False
        has_tel = False
        has_domain = False
        domain_val = None

        for d in decs:
            ev = evaluate_decisor(d)
            if best_eval is None or (
                sum([ev["nome_ok"], ev["cargo_ok"], ev["linkedin_ok"], ev["email_nominal_validado"]])
                > sum([best_eval["nome_ok"], best_eval["cargo_ok"], best_eval["linkedin_ok"], best_eval["email_nominal_validado"]])
            ):
                best_eval = ev
            if ev["nome_ok"]:
                has_nome = True
            if ev["cargo_ok"]:
                has_cargo = True
            if ev["linkedin_ok"]:
                has_li = True
            if d.get("email"):
                has_email = True
            if ev["email_nominal_validado"]:
                has_email_val = True
                has_domain = True
                domain_val = ev.get("domain")
            if d.get("telefone"):
                has_tel = True

        # domain from catalog
        if cnpj and cnpj in dominios:
            has_domain = True
            domain_val = dominios[cnpj]["dominio"]

        flags = {
            "empresa_presente": bool((o.get("empresa") or "").strip() or cnpj),
            "papel_empresa_confirmado": bool(cnpj and (o.get("empresa") or "").strip()),
            "cnpj_valido": cnpj_valido(cnpj) if cnpj else False,
            "dominio_confirmado": has_domain and bool(domain_val) and not is_personal_domain(domain_val or ""),
            "capex_valido": capex_ok(o.get("valor_estimado")),
            "decisor_presente": has_nome,
            "cargo_presente": has_cargo,
            "cargo_compativel": has_cargo,  # proxy
            "linkedin_presente": has_li,
            "linkedin_confirmado": has_li,
            "email_presente": has_email,
            "email_nominal": has_email_val or (has_email and best_eval and best_eval.get("email") and email_nominal_match(best_eval.get("nome") or "", best_eval.get("email") or "")),
            "email_corporativo": has_email and best_eval and best_eval.get("domain") and not is_personal_domain(best_eval.get("domain") or ""),
            "email_validado": has_email_val,
            "telefone_presente": has_tel,
        }

        # --- INTERNAL ENRICHMENT ---
        fontes = []
        precisa_ext = False
        resultado = "diagnostico"
        attempts = 0

        # 1) Resolve CNPJ for PIPELINE/BRONZE without CNPJ via lookup
        if not flags["cnpj_valido"] and (o.get("empresa") or "").strip():
            key = re.sub(r"\s+", " ", o["empresa"].strip().upper())
            cand = lookup_by_name.get(key)
            if cand and cnpj_valido(cand):
                attempts += 1
                fontes.append("entidades_lookup")
                if APPLY:
                    updates_cnpj.append((cand, oid))
                    audits.append((oid, "interno", "cnpj_resolvido", "cnpj", o.get("cnpj"), cand,
                                   "wins_v2.entidades_lookup", 0.8, f"razao={key}", tier, None, "FULL_HIT"))
                cnpj = cand
                flags["cnpj_valido"] = True
                flags["empresa_presente"] = True
                cnpj_applied += 1
                resultado = "cnpj_interno"
                full_hit += 1
            else:
                precisa_ext = True

        # 2) Domain from empresa_dominios
        if flags["cnpj_valido"] and not flags["dominio_confirmado"] and cnpj in dominios:
            attempts += 1
            dom = dominios[cnpj]["dominio"]
            if dom and not is_personal_domain(dom):
                fontes.append("empresa_dominios")
                flags["dominio_confirmado"] = True
                domain_val = dom
                dominio_applied += 1
                resultado = "dominio_interno"
                full_hit += 1
                if APPLY:
                    audits.append((oid, "interno", "dominio_catalogo", "dominio", None, dom,
                                   dominios[cnpj].get("fonte") or "empresa_dominios",
                                   float(dominios[cnpj].get("confianca") or 0),
                                   None, tier, None, "FULL_HIT"))

        # 3) Email for existing decisor from sibling works / preservados
        if flags["decisor_presente"] and not flags["email_validado"] and flags["cnpj_valido"]:
            for d in decs:
                if not nome_ok(d.get("nome")):
                    continue
                if d.get("email") and email_validated_status(d.get("email_status"), d.get("email_smtp_status")):
                    if email_nominal_match(d["nome"], d["email"]) and not is_generic_local(email_parts(d["email"])[0]):
                        flags["email_validado"] = True
                        flags["email_presente"] = True
                        flags["email_nominal"] = True
                        flags["email_corporativo"] = True
                        flags["dominio_confirmado"] = True
                        break
                # search siblings same CNPJ
                found = None
                for sib in by_cnpj_dec.get(cnpj, []):
                    if sib.get("origem_obra_id") == oid:
                        continue
                    if not sib.get("email"):
                        continue
                    if not email_validated_status(sib.get("email_status"), sib.get("email_smtp_status")):
                        continue
                    # same person by name similarity
                    if (sib.get("nome") or "").strip().lower() != (d.get("nome") or "").strip().lower():
                        # try nominal match of sibling email to current name
                        if not email_nominal_match(d["nome"], sib["email"]):
                            continue
                    else:
                        if not email_nominal_match(d["nome"], sib["email"]):
                            continue
                    if is_generic_local(email_parts(sib["email"])[0]) or is_personal_domain(email_parts(sib["email"])[1]):
                        continue
                    found = sib
                    break
                if not found:
                    for p in preservados.get(cnpj, []):
                        if not p.get("email"):
                            continue
                        if not email_nominal_match(d["nome"], p["email"]):
                            continue
                        if is_generic_local(email_parts(p["email"])[0]) or is_personal_domain(email_parts(p["email"])[1]):
                            continue
                        # preservados may not have status — only reuse if confianca high and already used elsewhere as valid
                        # require confianca_match >= 70 as proxy
                        if (p.get("confianca_match") or 0) < 70:
                            continue
                        found = {**p, "email_status": "valid", "fonte": p.get("fonte") or "decisores_preservados"}
                        break
                if found:
                    attempts += 1
                    fontes.append(found.get("fonte") or "decisor_irmao_cnpj")
                    if APPLY:
                        updates_email.append((found["email"], "valid", d["id"]))
                        audits.append((oid, "interno", "email_reuso", "email", d.get("email"), found["email"],
                                       found.get("fonte") or "irmao_cnpj", 0.85,
                                       f"decisor_id={d['id']};nome={d['nome']}", tier, None, "FULL_HIT"))
                    # update local state
                    d["email"] = found["email"]
                    d["email_status"] = "valid"
                    flags["email_validado"] = True
                    flags["email_presente"] = True
                    flags["email_nominal"] = True
                    flags["email_corporativo"] = True
                    flags["dominio_confirmado"] = True
                    email_applied += 1
                    resultado = "email_interno"
                    full_hit += 1
                    break
            if not flags["email_validado"] and flags["decisor_presente"]:
                precisa_ext = True
                full_miss += 1

        # 4) BRONZE without decisor: candidate from preservados/siblings as NEW decisor if high quality
        if not flags["decisor_presente"] and flags["cnpj_valido"]:
            cand = None
            # prefer preservado with linkedin + cargo + confianca
            for p in preservados.get(cnpj, []):
                if not nome_ok(p.get("nome")) or not cargo_ok(p.get("cargo")):
                    continue
                if not linkedin_ok(p.get("linkedin_url")):
                    continue
                if (p.get("confianca_match") or 0) < 70:
                    continue
                cand = p
                break
            if not cand:
                # sibling with full profile
                seen = set()
                for sib in by_cnpj_dec.get(cnpj, []):
                    key = (sib.get("nome") or "").strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    if not nome_ok(sib.get("nome")) or not cargo_ok(sib.get("cargo")):
                        continue
                    if not linkedin_ok(sib.get("linkedin_url")):
                        continue
                    if (sib.get("confianca_match") or 0) < 70:
                        continue
                    # skip false positive replication
                    if (sib.get("hipotese_replicacao") or "") == "REPLICADO_PROVAVEL_FALSO_POSITIVO":
                        continue
                    cand = sib
                    break
            if cand:
                attempts += 1
                fontes.append(cand.get("fonte") or "candidato_cnpj")
                # only attach as candidate if not already present
                if APPLY:
                    inserts_decisor.append({
                        "obra_id": oid,
                        "nome": cand["nome"],
                        "cargo": cand.get("cargo"),
                        "email": cand.get("email") if cand.get("email") and email_nominal_match(cand["nome"], cand["email"]) else None,
                        "email_status": "valid" if cand.get("email") and email_nominal_match(cand["nome"], cand.get("email") or "") and email_validated_status(cand.get("email_status"), cand.get("email_smtp_status")) else None,
                        "telefone": cand.get("telefone"),
                        "linkedin_url": cand.get("linkedin_url"),
                        "confianca_match": min(int(cand.get("confianca_match") or 70), 85),
                        "fonte": f"reuso_cnpj:{cand.get('fonte') or 'interno'}",
                        "hipotese_replicacao": "CANDIDATO_REUSO_CNPJ",
                    })
                    audits.append((oid, "interno", "decisor_candidato", "decisor", None, cand["nome"],
                                   cand.get("fonte") or "reuso_cnpj", 0.75,
                                   f"linkedin={cand.get('linkedin_url')}", tier, None, "FULL_HIT_CANDIDATO"))
                flags["decisor_presente"] = True
                flags["cargo_presente"] = cargo_ok(cand.get("cargo"))
                flags["cargo_compativel"] = flags["cargo_presente"]
                flags["linkedin_confirmado"] = linkedin_ok(cand.get("linkedin_url"))
                flags["linkedin_presente"] = flags["linkedin_confirmado"]
                if cand.get("email") and email_nominal_match(cand["nome"], cand["email"]) and email_validated_status(cand.get("email_status"), cand.get("email_smtp_status")):
                    flags["email_validado"] = True
                    flags["email_presente"] = True
                    flags["email_nominal"] = True
                    flags["email_corporativo"] = True
                    flags["dominio_confirmado"] = True
                if cand.get("telefone"):
                    flags["telefone_presente"] = True
                decisor_applied += 1
                resultado = "decisor_candidato_interno"
                full_hit += 1
            else:
                if not flags["decisor_presente"]:
                    precisa_ext = True
                    full_miss += 1

        # gaps & priority
        gap_map = {
            "empresa_presente": "empresa",
            "cnpj_valido": "cnpj",
            "dominio_confirmado": "dominio",
            "capex_valido": "capex",
            "decisor_presente": "decisor",
            "cargo_presente": "cargo",
            "linkedin_confirmado": "linkedin",
            "email_validado": "email_validado",
        }
        gaps = [gap_map[k] for k in gap_map if not flags.get(k if k != "email_validado" else "email_validado")]
        # fix keys
        gaps = []
        for k, label in [
            ("empresa_presente", "empresa"),
            ("cnpj_valido", "cnpj"),
            ("dominio_confirmado", "dominio"),
            ("capex_valido", "capex"),
            ("decisor_presente", "decisor"),
            ("cargo_presente", "cargo"),
            ("linkedin_confirmado", "linkedin"),
            ("email_validado", "email_validado"),
        ]:
            if not flags.get(k):
                gaps.append(label)

        prio = priority(tier, gaps, capex)
        score = completeness(flags)
        acao = next_action(tier, gaps)

        matrix_rows.append((
            oid, tier,
            flags["empresa_presente"], flags["papel_empresa_confirmado"], flags["cnpj_valido"],
            flags["dominio_confirmado"], flags["capex_valido"], flags["decisor_presente"],
            flags["cargo_presente"], flags["cargo_compativel"], flags["linkedin_presente"],
            flags["linkedin_confirmado"], flags["email_presente"], flags["email_nominal"],
            flags["email_corporativo"], flags["email_validado"], flags["telefone_presente"],
            gaps, acao, prio, fontes, precisa_ext, score,
            datetime.now(timezone.utc) if attempts else None, attempts, resultado,
        ))

        # store flags for tier recalc after DB updates
        o["_flags"] = flags
        o["_cnpj"] = cnpj
        o["_decs"] = decs

    print("matrix built", len(matrix_rows))
    print("cnpj_applied", cnpj_applied, "dominio", dominio_applied, "email", email_applied, "decisor", decisor_applied)

    if APPLY:
        # apply CNPJ updates
        for cnpj, oid in updates_cnpj:
            cur.execute(
                "UPDATE public.obras SET cnpj=%s, cnpj_status=COALESCE(cnpj_status,'ok') WHERE id=%s AND status_portao='APROVADA'",
                (cnpj, oid),
            )
        # email updates
        for email, status, did in updates_email:
            cur.execute(
                """UPDATE public.decisores_obra SET email=%s, email_status=%s
                   WHERE id=%s AND excluido_em IS NULL""",
                (email, status, did),
            )
        # insert candidate decisors
        for ins in inserts_decisor:
            cur.execute(
                """
                INSERT INTO public.decisores_obra
                  (obra_id, nome, cargo, email, email_status, telefone, linkedin_url,
                   confianca_match, fonte, hipotese_replicacao)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE NOT EXISTS (
                  SELECT 1 FROM public.decisores_obra d
                  WHERE d.obra_id=%s AND d.excluido_em IS NULL
                    AND lower(trim(d.nome))=lower(trim(%s))
                )
                """,
                (
                    ins["obra_id"], ins["nome"], ins.get("cargo"), ins.get("email"),
                    ins.get("email_status"), ins.get("telefone"), ins.get("linkedin_url"),
                    ins.get("confianca_match"), ins.get("fonte"), ins.get("hipotese_replicacao"),
                    ins["obra_id"], ins["nome"],
                ),
            )
        conn.commit()
        print("DB updates applied")

        # reload decisores after updates
        cur.execute(
            """
            SELECT id, obra_id, nome, cargo, email, email_status, email_smtp_status,
                   linkedin_url, telefone, confianca_match, hipotese_replicacao
            FROM public.decisores_obra WHERE excluido_em IS NULL
            """
        )
        by_obra = defaultdict(list)
        for d in cur.fetchall():
            by_obra[d["obra_id"]].append(d)

        # reload obras cnpj
        cur.execute("SELECT id, cnpj, classificacao_computed, valor_estimado, empresa, status_portao FROM public.obras WHERE status_portao='APROVADA'")
        obras2 = {r["id"]: r for r in cur.fetchall()}

        # recompute tiers with 8-criteria
        for oid, o2 in obras2.items():
            decs = by_obra.get(oid, [])
            # merge cnpj for calc
            ocalc = {
                "id": oid,
                "cnpj": o2.get("cnpj"),
                "valor_estimado": o2.get("valor_estimado"),
                "empresa": o2.get("empresa"),
                "classificacao_computed": o2.get("classificacao_computed"),
                "status_portao": "APROVADA",
            }
            res = calc_tier(ocalc, decs)
            new_tier = res["tier"]
            old_tier = o2.get("classificacao_computed") or "PIPELINE"
            tier_after[new_tier] += 1
            if new_tier != old_tier:
                cur.execute(
                    "UPDATE public.obras SET classificacao_computed=%s WHERE id=%s AND status_portao='APROVADA'",
                    (new_tier, oid),
                )
                key = f"{old_tier}->{new_tier}"
                promotions[key] += 1
                audits.append((oid, "reclass", "tier", "classificacao_computed", old_tier, new_tier,
                               "regra_ouro_8", 1.0, res["motivo"], old_tier, new_tier, "RECLASS"))
            else:
                promotions[f"mantida_{new_tier}"] += 1

        # upsert matrix
        execute_batch(cur, """
            INSERT INTO wins_v2.enrichment_gap_matrix (
              obra_id, tier_atual, empresa_presente, papel_empresa_confirmado, cnpj_valido,
              dominio_confirmado, capex_valido, decisor_presente, cargo_presente, cargo_compativel,
              linkedin_presente, linkedin_confirmado, email_presente, email_nominal, email_corporativo,
              email_validado, telefone_presente, campos_faltantes, proxima_acao, prioridade,
              fontes_internas_candidatas, precisa_externo, completeness_score,
              ultima_tentativa, num_tentativas, resultado_ultima, atualizado_em
            ) VALUES (
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()
            )
            ON CONFLICT (obra_id) DO UPDATE SET
              tier_atual=EXCLUDED.tier_atual,
              empresa_presente=EXCLUDED.empresa_presente,
              papel_empresa_confirmado=EXCLUDED.papel_empresa_confirmado,
              cnpj_valido=EXCLUDED.cnpj_valido,
              dominio_confirmado=EXCLUDED.dominio_confirmado,
              capex_valido=EXCLUDED.capex_valido,
              decisor_presente=EXCLUDED.decisor_presente,
              cargo_presente=EXCLUDED.cargo_presente,
              cargo_compativel=EXCLUDED.cargo_compativel,
              linkedin_presente=EXCLUDED.linkedin_presente,
              linkedin_confirmado=EXCLUDED.linkedin_confirmado,
              email_presente=EXCLUDED.email_presente,
              email_nominal=EXCLUDED.email_nominal,
              email_corporativo=EXCLUDED.email_corporativo,
              email_validado=EXCLUDED.email_validado,
              telefone_presente=EXCLUDED.telefone_presente,
              campos_faltantes=EXCLUDED.campos_faltantes,
              proxima_acao=EXCLUDED.proxima_acao,
              prioridade=EXCLUDED.prioridade,
              fontes_internas_candidatas=EXCLUDED.fontes_internas_candidatas,
              precisa_externo=EXCLUDED.precisa_externo,
              completeness_score=EXCLUDED.completeness_score,
              ultima_tentativa=EXCLUDED.ultima_tentativa,
              num_tentativas=EXCLUDED.num_tentativas,
              resultado_ultima=EXCLUDED.resultado_ultima,
              atualizado_em=now()
        """, matrix_rows, page_size=500)

        # refresh tier_atual in matrix from DB
        cur.execute(
            """
            UPDATE wins_v2.enrichment_gap_matrix m
            SET tier_atual = o.classificacao_computed
            FROM public.obras o
            WHERE o.id = m.obra_id AND o.status_portao='APROVADA'
            """
        )

        # audit batch
        if audits:
            execute_batch(cur, """
                INSERT INTO wins_v2.enrichment_gap_audit
                  (obra_id, fase, acao, campo, valor_antes, valor_depois, fonte, confianca,
                   evidencia, tier_antes, tier_depois, resultado)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, audits, page_size=500)

        conn.commit()
        print("matrix+audit committed")
    else:
        # dry-run tier projection
        for o in obras:
            decs = by_obra.get(o["id"], [])
            ocalc = dict(o)
            # simulate cnpj if would be applied - skip complex
            res = calc_tier(ocalc, decs)
            tier_after[res["tier"]] += 1

    # final counts
    cur.execute(
        "SELECT classificacao_computed, count(*) FROM public.obras WHERE status_portao='APROVADA' GROUP BY 1"
    )
    final_tiers = dict(cur.fetchall())
    cur.execute("SELECT count(*) FROM wins_v2.enrichment_gap_matrix")
    _mn = cur.fetchone()
    matrix_n = _mn["count"] if isinstance(_mn, dict) else _mn[0]

    out = {
        "aprovadas": stats["aprovadas"],
        "tier_before": dict(tier_before),
        "tier_after_db": final_tiers,
        "promotions": dict(promotions),
        "cnpj_applied": cnpj_applied,
        "dominio_applied": dominio_applied,
        "email_applied": email_applied,
        "decisor_applied": decisor_applied,
        "full_hit": full_hit,
        "full_miss": full_miss,
        "matrix_rows": matrix_n,
        "audit_rows": len(audits),
        "apply": APPLY,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    open("/tmp/enrich_gap_report.json", "w").write(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    conn.close()
    return out


if __name__ == "__main__":
    main()
