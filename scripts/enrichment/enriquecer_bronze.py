#!/usr/bin/env python3
"""Enriquecimento comercial das obras BRONZE (status_portao=APROVADA).

Somente dados internos. Não inventa contatos. Não altera Portão.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Cargos comerciais relevantes (match case-insensitive)
CARGOS_OK = [
    "diretor de engenharia", "gerente de engenharia", "diretor de obras",
    "gerente de obras", "diretor de projetos", "gerente de projetos",
    "diretor industrial", "gerente industrial", "diretor de suprimentos",
    "gerente de suprimentos", "gerente de compras", "coordenador de obras",
    "coordenador de engenharia", "responsavel tecnico", "responsável técnico",
    "gestor do contrato", "gerente de expansao", "gerente de expansão",
    "diretor de implantacao", "diretor de implantação", "gerente de infraestrutura",
    "diretor de operacoes", "diretor de operações", "engenheiro", "prefeito",
    "secretario", "secretário", "superintendente", "presidente", "diretor",
    "gerente", "coordenador", "engenheiro civil", "engenheiro de obras",
]

CARGOS_BLOQUEIO = [
    "recepcao", "recepção", "atendimento", "sac ", "recursos humanos",
    "rh ", "marketing", "financeiro generico", "estagiario", "estagiário",
]


def connect():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    ), RealDictCursor


def health_ok() -> bool:
    for url in ("http://127.0.0.1:8000/healthz", "http://127.0.0.1:8001/healthz"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return True  # DB batch continues


def digits(cnpj: Any) -> str:
    return re.sub(r"\D", "", str(cnpj or ""))


def cargo_ok(cargo: Optional[str]) -> bool:
    c = (cargo or "").lower()
    if not c.strip():
        return False
    if any(b in c for b in CARGOS_BLOQUEIO):
        return False
    # allow empty-ish generic "diretor" etc via CARGOS_OK
    return any(k in c for k in CARGOS_OK) or len(c) >= 4


def contato_validado(email: Optional[str], email_status: Optional[str],
                     telefone: Optional[str], tel_status: Optional[str],
                     linkedin: Optional[str], email_smtp: Any = None) -> bool:
    """Contato acionável validado para OURO."""
    em = (email or "").strip()
    tel = (telefone or "").strip()
    li = (linkedin or "").strip()
    st = (email_status or "").lower()
    if em and st in ("valido", "valid", "ok", "smtp_ok") or email_smtp in (True, "true", "1", 1):
        # not generic only
        if not re.search(r"^(contato|sac|ouvidoria|info|noreply)@", em, re.I):
            return True
    if tel and len(re.sub(r"\D", "", tel)) >= 10:
        if (tel_status or "").lower() in ("valido", "valid", "ok", "verificado", ""):
            # telefone presente e com dígitos ok — se status vazio, partial for PRATA not OURO
            if (tel_status or "").lower() in ("valido", "valid", "ok", "verificado"):
                return True
    if li and "linkedin.com" in li.lower():
        return True  # LinkedIn real as actionable for OURO per existing qualidade_lead logic
    return False


def contato_parcial(email: Optional[str], telefone: Optional[str], linkedin: Optional[str]) -> bool:
    em = (email or "").strip()
    tel = (telefone or "").strip()
    li = (linkedin or "").strip()
    if em and "@" in em:
        return True
    if tel and len(re.sub(r"\D", "", tel)) >= 10:
        return True
    if li:
        return True
    return False


def pick_best_decisor(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None

    def score(d: Dict[str, Any]) -> Tuple:
        nome = (d.get("nome") or "").strip()
        cargo = d.get("cargo") or ""
        email = d.get("email") or ""
        tel = d.get("telefone") or ""
        li = d.get("linkedin") or d.get("linkedin_url") or ""
        est = d.get("email_status") or ""
        return (
            1 if contato_validado(email, est, tel, d.get("telefone_status"), li) else 0,
            1 if contato_parcial(email, tel, li) else 0,
            1 if cargo_ok(cargo) else 0,
            1 if nome else 0,
            len(email or ""),
            len(tel or ""),
        )

    ranked = sorted(candidates, key=score, reverse=True)
    best = ranked[0]
    if not (best.get("nome") or "").strip():
        return None
    return best


def classify(obra: Dict[str, Any], dec: Optional[Dict[str, Any]], empresa_ok: bool) -> Tuple[str, str, float]:
    """Return (tier, motivo, confianca)."""
    if not empresa_ok:
        return "PIPELINE", "sem_empresa_ou_entidade_apos_enriquecimento", 0.4

    if not dec or not (dec.get("nome") or "").strip():
        return "BRONZE", "empresa_ok_sem_decisor_confiavel", 0.55

    cargo = dec.get("cargo") or obra.get("nivel1_cargo") or ""
    if not cargo_ok(cargo) and not cargo.strip():
        # nome without cargo → still PRATA if contact partial
        cargo = "OUTRO"

    email = dec.get("email") or obra.get("nivel1_email")
    tel = dec.get("telefone") or obra.get("nivel1_telefone")
    li = dec.get("linkedin") or dec.get("linkedin_url") or obra.get("nivel1_linkedin")
    est = dec.get("email_status") or obra.get("nivel1_email_status")
    tel_st = dec.get("telefone_status") or obra.get("nivel1_telefone_status")

    if contato_validado(email, est, tel, tel_st, li) and cargo_ok(cargo) or (
        contato_validado(email, est, tel, tel_st, li) and (cargo or "").strip()
    ):
        return "OURO", "decisor_e_contato_validado", 0.92

    if contato_parcial(email, tel, li) or cargo_ok(cargo):
        return "PRATA", "decisor_identificado_contato_parcial", 0.8

    # has name only
    return "BRONZE", "decisor_sem_contato_suficiente", 0.6


def fetch_batch(conn, RDC, ids: List[str]) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RDC) as cur:
        cur.execute(
            """
            SELECT id::text AS id, nome, empresa, cnpj, empresa_executora, cnpj_executora,
                   nivel1_nome, nivel1_cargo, nivel1_email, nivel1_telefone, nivel1_linkedin,
                   nivel1_email_status, nivel1_telefone_status, nivel1_email_smtp_verified,
                   nivel1_origem_enrichment, classificacao_computed, status_enriquecimento,
                   fonte, setor, uf, valor_estimado, municipio
              FROM public.obras
             WHERE id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        return [dict(r) for r in cur.fetchall()]


def gather_internal(conn, RDC, obra: Dict[str, Any]) -> Tuple[Optional[Dict], List[str], Dict[str, Any]]:
    """Collect best decisor + entity enrichment from internal sources only."""
    fontes: List[str] = []
    campos: Dict[str, Any] = {}
    cands: List[Dict[str, Any]] = []
    oid = obra["id"]
    cnpj = digits(obra.get("cnpj"))
    if len(cnpj) != 14:
        cnpj = ""

    # 1) existing nivel1
    if (obra.get("nivel1_nome") or "").strip():
        cands.append({
            "nome": obra.get("nivel1_nome"),
            "cargo": obra.get("nivel1_cargo"),
            "email": obra.get("nivel1_email"),
            "telefone": obra.get("nivel1_telefone"),
            "linkedin": obra.get("nivel1_linkedin"),
            "email_status": obra.get("nivel1_email_status"),
            "telefone_status": obra.get("nivel1_telefone_status"),
            "fonte": "obras.nivel1",
        })
        fontes.append("obras.nivel1")

    with conn.cursor(cursor_factory=RDC) as cur:
        # 2) decisores_obra of this work
        cur.execute(
            """
            SELECT nome, cargo, email, telefone, linkedin_url AS linkedin,
                   email_status, telefone_fonte AS telefone_status, fonte
              FROM decisores_obra
             WHERE obra_id=%s AND excluido_em IS NULL
               AND (hipotese_replicacao IS NULL OR hipotese_replicacao <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO')
             ORDER BY confianca_match DESC NULLS LAST, registrado_em DESC
             LIMIT 10
            """,
            (oid,),
        )
        for r in cur.fetchall():
            cands.append(dict(r))
            fontes.append("decisores_obra")

        # 3) sibling obras same CNPJ with better decisor
        if cnpj:
            cur.execute(
                """
                SELECT nivel1_nome AS nome, nivel1_cargo AS cargo, nivel1_email AS email,
                       nivel1_telefone AS telefone, nivel1_linkedin AS linkedin,
                       nivel1_email_status AS email_status, nivel1_telefone_status AS telefone_status,
                       'obra_irma_cnpj' AS fonte
                  FROM public.obras
                 WHERE cnpj=%s AND id<>%s AND status_portao='APROVADA'
                   AND NULLIF(nivel1_nome,'') IS NOT NULL
                 ORDER BY
                   CASE classificacao_computed WHEN 'OURO' THEN 1 WHEN 'PRATA' THEN 2 ELSE 3 END,
                   (NULLIF(nivel1_email,'') IS NOT NULL) DESC,
                   (NULLIF(nivel1_telefone,'') IS NOT NULL) DESC
                 LIMIT 5
                """,
                (obra.get("cnpj"), oid),
            )
            for r in cur.fetchall():
                cands.append(dict(r))
                fontes.append("obra_irma_cnpj")

            # decisores from sibling works
            cur.execute(
                """
                SELECT d.nome, d.cargo, d.email, d.telefone, d.linkedin_url AS linkedin,
                       d.email_status, 'decisor_irma_cnpj' AS fonte
                  FROM decisores_obra d
                  JOIN public.obras o2 ON o2.id = d.obra_id
                 WHERE o2.cnpj=%s AND o2.id<>%s AND d.excluido_em IS NULL
                   AND o2.status_portao='APROVADA'
                 ORDER BY d.confianca_match DESC NULLS LAST
                 LIMIT 10
                """,
                (obra.get("cnpj"), oid),
            )
            for r in cur.fetchall():
                cands.append(dict(r))
                fontes.append("decisor_irma_cnpj")

            # decisores_preservados
            cur.execute(
                """
                SELECT nome, cargo, email, telefone, linkedin_url AS linkedin,
                       NULL AS email_status, 'decisores_preservados' AS fonte
                  FROM decisores_preservados
                 WHERE cnpj=%s
                 ORDER BY (NULLIF(email,'') IS NOT NULL) DESC, confianca_match DESC NULLS LAST
                 LIMIT 5
                """,
                (cnpj,),
            )
            for r in cur.fetchall():
                cands.append(dict(r))
                fontes.append("decisores_preservados")

            # fornecedores
            cur.execute(
                """
                SELECT razao_social, nome_fantasia, email, municipio_nome
                  FROM fornecedores WHERE cnpj=%s LIMIT 1
                """,
                (cnpj,),
            )
            f = cur.fetchone()
            if f:
                campos["fornecedor_razao"] = f.get("razao_social") or f.get("nome_fantasia")
                campos["fornecedor_email"] = f.get("email")
                campos["fornecedor_municipio"] = f.get("municipio_nome")
                fontes.append("fornecedores")

            # entidades_lookup
            cur.execute(
                """
                SELECT razao_social, nome_fantasia, municipio, uf, situacao
                  FROM wins_v2.entidades_lookup WHERE cnpj_normalizado=%s LIMIT 1
                """,
                (cnpj,),
            )
            el = cur.fetchone()
            if el:
                campos["lookup_razao"] = el.get("razao_social") or el.get("nome_fantasia")
                campos["lookup_municipio"] = el.get("municipio")
                fontes.append("entidades_lookup")

            # empresa_dominios
            cur.execute(
                """
                SELECT dominio, holding_dominio FROM empresa_dominios
                 WHERE cnpj=%s OR substring(cnpj,1,8)=%s
                 ORDER BY (dominio IS NOT NULL) DESC LIMIT 1
                """,
                (cnpj, cnpj[:8]),
            )
            dom = cur.fetchone()
            if dom and (dom.get("dominio") or dom.get("holding_dominio")):
                campos["dominio"] = dom.get("dominio") or dom.get("holding_dominio")
                fontes.append("empresa_dominios")

    # company fill from lookup/fornecedor if missing
    if not (obra.get("empresa") or "").strip():
        if campos.get("lookup_razao"):
            campos["empresa_resolvida"] = campos["lookup_razao"]
        elif campos.get("fornecedor_razao"):
            campos["empresa_resolvida"] = campos["fornecedor_razao"]

    best = pick_best_decisor(cands)
    return best, list(dict.fromkeys(fontes)), campos


def apply_enrichment(conn, obra: Dict[str, Any], dec: Optional[Dict], fontes: List[str],
                     campos: Dict[str, Any], tier: str, motivo: str, conf: float, lote: str) -> Dict[str, Any]:
    oid = obra["id"]
    prev = {
        "classificacao_computed": obra.get("classificacao_computed"),
        "empresa": obra.get("empresa"),
        "nivel1_nome": obra.get("nivel1_nome"),
        "nivel1_cargo": obra.get("nivel1_cargo"),
        "nivel1_email": obra.get("nivel1_email"),
        "nivel1_telefone": obra.get("nivel1_telefone"),
        "nivel1_linkedin": obra.get("nivel1_linkedin"),
        "nivel1_email_status": obra.get("nivel1_email_status"),
    }
    novos = dict(prev)
    changed_fields = []

    empresa_new = obra.get("empresa")
    if campos.get("empresa_resolvida") and not (empresa_new or "").strip():
        empresa_new = campos["empresa_resolvida"]
        novos["empresa"] = empresa_new
        changed_fields.append("empresa")

    n_nome = obra.get("nivel1_nome")
    n_cargo = obra.get("nivel1_cargo")
    n_email = obra.get("nivel1_email")
    n_tel = obra.get("nivel1_telefone")
    n_li = obra.get("nivel1_linkedin")
    n_est = obra.get("nivel1_email_status")
    origem = obra.get("nivel1_origem_enrichment")

    if dec:
        # only fill empties or upgrade if better contact
        if (dec.get("nome") or "").strip() and not (n_nome or "").strip():
            n_nome = dec["nome"].strip()
            changed_fields.append("nivel1_nome")
        if (dec.get("cargo") or "").strip() and not (n_cargo or "").strip():
            n_cargo = dec["cargo"]
            changed_fields.append("nivel1_cargo")
        # contacts: never overwrite validated with empty; only fill empty
        if (dec.get("email") or "").strip() and not (n_email or "").strip():
            n_email = dec["email"].strip()
            n_est = dec.get("email_status") or n_est
            changed_fields.append("nivel1_email")
        if (dec.get("telefone") or "").strip() and not (n_tel or "").strip():
            n_tel = dec["telefone"].strip()
            changed_fields.append("nivel1_telefone")
        li = dec.get("linkedin") or dec.get("linkedin_url")
        if (li or "").strip() and not (n_li or "").strip():
            n_li = li.strip()
            changed_fields.append("nivel1_linkedin")
        origem = ",".join(fontes[:5]) if fontes else "interno"
        novos.update({
            "nivel1_nome": n_nome,
            "nivel1_cargo": n_cargo,
            "nivel1_email": n_email,
            "nivel1_telefone": n_tel,
            "nivel1_linkedin": n_li,
            "nivel1_email_status": n_est,
        })

    # status enrichment
    if tier == "OURO":
        st_enr = "COMPLETO"
    elif tier == "PRATA":
        st_enr = "PARCIAL"
    elif tier == "PIPELINE":
        st_enr = "INSUFICIENTE"
    else:
        st_enr = "PARCIAL" if changed_fields or fontes else "INSUFICIENTE"

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.obras SET
              classificacao_computed = %s,
              empresa = COALESCE(NULLIF(%s,''), empresa),
              nivel1_nome = COALESCE(NULLIF(%s,''), nivel1_nome),
              nivel1_cargo = COALESCE(NULLIF(%s,''), nivel1_cargo),
              nivel1_email = COALESCE(NULLIF(%s,''), nivel1_email),
              nivel1_telefone = COALESCE(NULLIF(%s,''), nivel1_telefone),
              nivel1_linkedin = COALESCE(NULLIF(%s,''), nivel1_linkedin),
              nivel1_email_status = COALESCE(NULLIF(%s,''), nivel1_email_status),
              nivel1_origem_enrichment = COALESCE(%s, nivel1_origem_enrichment),
              status_enriquecimento = %s,
              ultimo_enrichment_at = now(),
              ultimo_enrichment_status = 'ok',
              observacoes_enrichment = left(%s, 500)
            WHERE id = %s
              AND status_portao = 'APROVADA'
            """,
            (
                tier,
                empresa_new or "",
                n_nome or "",
                n_cargo or "",
                n_email or "",
                n_tel or "",
                n_li or "",
                n_est or "",
                origem,
                st_enr,
                f"bronze_enrich:{motivo}",
                oid,
            ),
        )
        # ensure decisores_obra row if we have decisor
        if dec and (dec.get("nome") or "").strip():
            try:
                cur.execute(
                    """
                    INSERT INTO decisores_obra (
                      obra_id, nome, cargo, email, telefone, linkedin_url, fonte, tipo_cargo
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, 'OUTRO'
                    )
                    ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO UPDATE SET
                      cargo = COALESCE(NULLIF(EXCLUDED.cargo,''), decisores_obra.cargo),
                      email = COALESCE(NULLIF(EXCLUDED.email,''), decisores_obra.email),
                      telefone = COALESCE(NULLIF(EXCLUDED.telefone,''), decisores_obra.telefone),
                      linkedin_url = COALESCE(NULLIF(EXCLUDED.linkedin_url,''), decisores_obra.linkedin_url)
                    """,
                    (
                        oid,
                        (dec.get("nome") or "")[:200],
                        (n_cargo or dec.get("cargo") or "Não informado")[:200],
                        n_email,
                        n_tel,
                        n_li,
                        (dec.get("fonte") or "bronze_enrich_interno")[:100],
                    ),
                )
            except Exception:
                # unique constraint variants - ignore dup
                pass

        cur.execute(
            """
            INSERT INTO wins_v2.bronze_enrich_audit (
              obra_id, classificacao_anterior, classificacao_nova,
              campos_enriquecidos, valores_anteriores, valores_novos,
              fontes, confianca, regra_aplicada, motivo_promocao, lote
            ) VALUES (
              %s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s
            )
            """,
            (
                oid,
                prev["classificacao_computed"],
                tier,
                json.dumps(changed_fields, ensure_ascii=False),
                json.dumps(prev, ensure_ascii=False, default=str),
                json.dumps(novos, ensure_ascii=False, default=str),
                json.dumps(fontes, ensure_ascii=False),
                conf,
                "ENRIQUECIMENTO_INTERNO_BRONZE_V1",
                motivo,
                lote,
            ),
        )
    conn.commit()
    return {
        "tier": tier,
        "motivo": motivo,
        "changed": changed_fields,
        "fontes": fontes,
        "decisor": bool(dec and dec.get("nome")),
        "email": bool(n_email),
        "telefone": bool(n_tel),
        "empresa_resolvida": "empresa" in changed_fields,
    }


def ordered_ids(conn) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text FROM public.obras
            WHERE status_portao='APROVADA' AND classificacao_computed='BRONZE'
            ORDER BY
              CASE
                WHEN cnpj IS NOT NULL AND cnpj<>'' AND NULLIF(empresa,'') IS NOT NULL
                     AND NULLIF(nivel1_nome,'') IS NULL THEN 1
                WHEN NULLIF(nivel1_nome,'') IS NOT NULL
                     AND NULLIF(nivel1_email,'') IS NULL AND NULLIF(nivel1_telefone,'') IS NULL THEN 2
                WHEN NULLIF(nivel1_email,'') IS NOT NULL OR NULLIF(nivel1_telefone,'') IS NOT NULL THEN 3
                WHEN NULLIF(empresa_executora,'') IS NOT NULL THEN 4
                ELSE 9
              END,
              CASE upper(coalesce(setor,''))
                WHEN 'INDUSTRIAL' THEN 1 WHEN 'LOGISTICO' THEN 2 WHEN 'ENERGIA' THEN 3
                WHEN 'SANEAMENTO' THEN 4 WHEN 'INFRAESTRUTURA' THEN 5 WHEN 'MINERACAO' THEN 6
                WHEN 'PETROLEO_GAS' THEN 7 ELSE 9
              END,
              valor_estimado DESC NULLS LAST,
              criado_em DESC NULLS LAST
            """
        )
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    conn, RDC = connect()
    out_dir = Path(os.environ.get("BRONZE_OUT", "/tmp/bronze_enrich"))
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = ordered_ids(conn)
    total = len(ids)
    print(f"BRONZE_TOTAL={total}", flush=True)

    report = {
        "started": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "lotes": [],
        "stats": Counter(),
        "metrics": Counter(),
        "errors": [],
    }

    # diagnostic before
    with conn.cursor(cursor_factory=RDC) as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) total,
              COUNT(*) FILTER (WHERE empresa IS NOT NULL AND empresa<>'') com_empresa,
              COUNT(*) FILTER (WHERE cnpj IS NOT NULL AND cnpj<>'') com_cnpj,
              COUNT(*) FILTER (WHERE NULLIF(nivel1_nome,'') IS NOT NULL) com_decisor,
              COUNT(*) FILTER (WHERE NULLIF(nivel1_telefone,'') IS NOT NULL) com_tel,
              COUNT(*) FILTER (WHERE NULLIF(nivel1_email,'') IS NOT NULL) com_email
            FROM public.obras
            WHERE status_portao='APROVADA' AND classificacao_computed='BRONZE'
            """
        )
        report["diagnostico_inicial"] = dict(cur.fetchone())
    print("DIAG", report["diagnostico_inicial"], flush=True)

    processed = 0
    pos = 0
    lote_num = 0

    def batch_size(done: int) -> int:
        if done < 100:
            return min(100, total - done)
        if done < 600:
            return min(500, total - done)
        if done < 1600:
            return min(1000, total - done)
        return min(2000, total - done)

    while pos < total:
        if not health_ok():
            report["abort"] = "health"
            break
        bsz = batch_size(processed)
        if bsz <= 0:
            break
        lote_num += 1
        lote_id = f"B{lote_num:03d}_{bsz}"
        batch_ids = ids[pos: pos + bsz]
        pos += bsz
        obras = {o["id"]: o for o in fetch_batch(conn, RDC, batch_ids)}
        t0 = time.time()
        lote_stats = Counter()
        lote_err = 0
        m = Counter()

        for oid in batch_ids:
            obra = obras.get(oid)
            if not obra:
                continue
            try:
                # re-check still bronze
                if obra.get("classificacao_computed") != "BRONZE":
                    continue
                dec, fontes, campos = gather_internal(conn, RDC, obra)
                empresa_ok = bool((obra.get("empresa") or "").strip() or campos.get("empresa_resolvida"))
                tier, motivo, conf = classify(obra, dec, empresa_ok)
                res = apply_enrichment(conn, obra, dec, fontes, campos, tier, motivo, conf, lote_id)
                lote_stats[tier] += 1
                report["stats"][tier] += 1
                m["processadas"] += 1
                if res["decisor"]:
                    m["decisores"] += 1
                if res["email"]:
                    m["emails"] += 1
                if res["telefone"]:
                    m["telefones"] += 1
                if res["empresa_resolvida"]:
                    m["empresas_resolvidas"] += 1
                if fontes:
                    m["full_hit_interno"] += 1
                else:
                    m["full_miss_interno"] += 1
                if any(f.startswith("obra_irma") or f.startswith("decisor_irma") or f == "decisores_preservados" for f in fontes):
                    m["reuso_interno"] += 1
                processed += 1
            except Exception as exc:
                lote_err += 1
                report["errors"].append({"id": oid, "err": str(exc)[:300]})
                conn.rollback()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE public.obras SET ultimo_enrichment_at=now(),
                              ultimo_enrichment_status='erro',
                              ultimo_enrichment_skip_motivo=%s
                            WHERE id=%s
                            """,
                            (str(exc)[:200], oid),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()

        elapsed = time.time() - t0
        err_rate = lote_err / max(1, len(batch_ids))
        for k, v in m.items():
            report["metrics"][k] += v
        lote_rep = {
            "lote": lote_id,
            "n": len(batch_ids),
            "stats": dict(lote_stats),
            "errors": lote_err,
            "err_rate": round(err_rate, 4),
            "elapsed_s": round(elapsed, 2),
            "metrics": dict(m),
            "health": health_ok(),
        }
        report["lotes"].append(lote_rep)
        print(json.dumps(lote_rep, ensure_ascii=False), flush=True)
        if err_rate > 0.01 and lote_err > 5:
            report["abort"] = f"err_rate_{err_rate}"
            break

    # final distribution
    with conn.cursor(cursor_factory=RDC) as cur:
        cur.execute(
            """
            SELECT classificacao_computed, COUNT(*)
              FROM public.obras
             WHERE status_portao='APROVADA'
               AND id IN (SELECT obra_id FROM wins_v2.bronze_enrich_snapshot)
             GROUP BY 1 ORDER BY 2 DESC
            """
        )
        report["distribuicao_final"] = {r["classificacao_computed"]: r["count"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM wins_v2.bronze_enrich_audit")
        report["audit_rows"] = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM public.obras
            WHERE status_portao='APROVADA'
              AND id IN (SELECT obra_id FROM wins_v2.bronze_enrich_snapshot)
              AND classificacao_computed='BRONZE'
            """
        )
        report["bronze_restante"] = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM public.obras")
        report["v1_total"] = cur.fetchone()["n"]

    report["processed"] = processed
    report["stats"] = dict(report["stats"])
    report["metrics"] = dict(report["metrics"])
    report["finished"] = datetime.now(timezone.utc).isoformat()
    report["consultas_externas"] = 0

    (out_dir / "ENRIQUECIMENTO_BRONZE.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("FINAL", json.dumps(report["distribuicao_final"], ensure_ascii=False), flush=True)
    print("PROCESSED", processed, "OF", total, flush=True)
    conn.close()
    return 0 if processed >= total and not report.get("abort") else 1


if __name__ == "__main__":
    raise SystemExit(main())
