#!/usr/bin/env python3
"""Auditoria e reclassificação de OURO sem canal acionável validado.

Regras:
- OURO só com canal acionável validado (tel direto, celular corp, WhatsApp, e-mail nominal, depto validado)
- Telefone de alta frequência (≥10 obras) = GERAL_EMPRESA
- LinkedIn isolado = PRATA
- Não inventa dados; registra auditoria e rollback
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

TEL_FREQ_GERAL = 10  # mesmo número em ≥10 obras → geral
NOME_FREQ_REPLICADO = 30  # mesmo nome de decisor em ≥30 obras → reuso suspeito


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "wins_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def email_generico(em: str) -> bool:
    local = (em or "").split("@")[0].lower()
    return local in {
        "contato", "sac", "ouvidoria", "info", "informacoes", "informações",
        "noreply", "no-reply", "admin", "suporte", "financeiro", "rh",
        "atendimento", "comercial", "vendas", "secretaria", "protocolo",
    }


def classificar_canal(
    *,
    email: Optional[str],
    email_status: Optional[str],
    email_smtp: Optional[str],
    telefone: Optional[str],
    telefone_fonte: Optional[str],
    whatsapp_status: Optional[str],
    linkedin: Optional[str],
    tel_freq: int,
    nome_freq: int,
    confianca: Optional[int],
) -> Tuple[str, Dict[str, Any]]:
    """Retorna (tipo_canal, meta)."""
    em = (email or "").strip()
    tel = digits(telefone)
    li = (linkedin or "").strip()
    est = (email_status or "").lower()
    smtp = (email_smtp or "").lower()
    wa = (whatsapp_status or "").lower()
    tf = (telefone_fonte or "").lower()

    # E-mail nominal validado
    if em and "@" in em and not email_generico(em):
        if est in ("valido", "valid", "ok", "smtp_ok") or smtp in ("ok", "valid", "valido", "true"):
            return "EMAIL_NOMINAL_VALIDADO", {"email": em, "status": est or smtp}
        if est in ("invalid", "invalido", "bounce"):
            pass  # fall through
        else:
            # e-mail sem validação → não é OURO sozinho
            email_parcial = em
    else:
        email_parcial = None
        if em and email_generico(em):
            email_parcial = em

    # WhatsApp confirmado
    if tel and len(tel) >= 10 and wa in ("confirmado", "confirmed", "ativo", "sim", "ok", "validado"):
        return "WHATSAPP_CONFIRMADO", {"telefone": tel, "whatsapp_status": wa}

    # Telefone
    if tel and len(tel) >= 10:
        if tel_freq >= TEL_FREQ_GERAL:
            return "GERAL_EMPRESA", {
                "telefone": tel,
                "freq_obras": tel_freq,
                "motivo": f"telefone repetido em {tel_freq} obras",
            }
        # celular BR: 11 dígitos com 9 após DDD
        is_cel = len(tel) == 11 and tel[2] == "9"
        if "direto" in tf or "pessoal" in tf or "ramal" in tf:
            return ("CELULAR_CORPORATIVO_VALIDADO" if is_cel else "DIRETO_VALIDADO"), {
                "telefone": tel,
                "telefone_fonte": tf,
                "freq_obras": tel_freq,
            }
        if "departamento" in tf or "setor" in tf:
            return "DEPARTAMENTO_VALIDADO", {"telefone": tel, "telefone_fonte": tf}
        if confianca and confianca >= 80 and tel_freq <= 2 and nome_freq < NOME_FREQ_REPLICADO:
            # ainda assim sem validação explícita → não validado
            return "NAO_VALIDADO", {
                "telefone": tel,
                "freq_obras": tel_freq,
                "nome_freq": nome_freq,
                "motivo": "telefone sem status de validação explícito",
            }
        if nome_freq >= NOME_FREQ_REPLICADO:
            return "INFERIDO", {
                "telefone": tel,
                "nome_freq": nome_freq,
                "motivo": "decisor massivamente replicado",
            }
        return "NAO_VALIDADO", {"telefone": tel, "freq_obras": tel_freq}

    if email_parcial:
        if email_generico(email_parcial):
            return "EMAIL_GERAL", {"email": email_parcial}
        return "INFERIDO", {"email": email_parcial, "motivo": "e-mail sem validação"}

    if li:
        return "LINKEDIN_SOMENTE", {"linkedin": li}

    return "SEM_CANAL", {}


CANAIS_OURO = {
    "DIRETO_VALIDADO",
    "CELULAR_CORPORATIVO_VALIDADO",
    "WHATSAPP_CONFIRMADO",
    "EMAIL_NOMINAL_VALIDADO",
    "DEPARTAMENTO_VALIDADO",
}


def build_justificativa(tier: str, canal: str, meta: Dict, tem_empresa: bool, tem_decisor: bool, tem_cargo: bool) -> Dict[str, Any]:
    atendidos = []
    pendentes = []
    atendidos.append("Obra confirmada (Portão APROVADA)")
    if tem_empresa:
        atendidos.append("Empresa identificada")
    else:
        pendentes.append("Empresa/entidade comercial")
    if tem_decisor:
        atendidos.append("Decisor compatível")
    else:
        pendentes.append("Decisor confiável")
    if tem_cargo:
        atendidos.append("Cargo compatível")
    else:
        pendentes.append("Cargo compatível")

    canal_label = {
        "DIRETO_VALIDADO": "Telefone direto validado",
        "CELULAR_CORPORATIVO_VALIDADO": "Celular corporativo validado",
        "WHATSAPP_CONFIRMADO": "WhatsApp confirmado",
        "EMAIL_NOMINAL_VALIDADO": "E-mail corporativo nominal validado",
        "DEPARTAMENTO_VALIDADO": "Contato de departamento validado",
        "GERAL_EMPRESA": "Telefone geral da organização",
        "EMAIL_GERAL": "E-mail geral da organização",
        "LINKEDIN_SOMENTE": "LinkedIn (perfil profissional)",
        "INFERIDO": "Contato inferido / não validado",
        "NAO_VALIDADO": "Telefone sem validação plena",
        "SEM_CANAL": "Sem canal de contato",
        "INVALIDO": "Contato inválido",
    }.get(canal, canal)

    if canal in CANAIS_OURO:
        atendidos.append(canal_label)
    else:
        if canal != "SEM_CANAL":
            pendentes.append(canal_label + " — não basta para OURO")
        else:
            pendentes.append("Canal acionável validado")

    titulos = {
        "OURO": "OURO — Pronta para contato",
        "PRATA": "PRATA — Decisor identificado · contato parcial",
        "BRONZE": "BRONZE — Empresa identificada · em validação",
        "PIPELINE": "PIPELINE — Aguardando enriquecimento",
    }
    return {
        "tier": tier,
        "titulo": titulos.get(tier, tier),
        "criterios_atendidos": atendidos,
        "criterios_pendentes": pendentes,
        "canal_principal": {"tipo": canal, "label": canal_label, **{k: v for k, v in meta.items() if k != "motivo"}},
        "confianca": 0.95 if canal in CANAIS_OURO else 0.7 if tem_decisor else 0.5,
    }


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS wins_v2.tier_coerencia_audit (
              id bigserial PRIMARY KEY,
              obra_id uuid NOT NULL,
              tier_anterior text,
              tier_novo text,
              motivo text,
              telefone text,
              tipo_telefone text,
              fonte text,
              confianca numeric,
              decisor text,
              cargo text,
              canal_meta jsonb,
              regra_aplicada text,
              criado_em timestamptz DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_tier_coerencia_obra ON wins_v2.tier_coerencia_audit(obra_id);
            GRANT SELECT, INSERT ON wins_v2.tier_coerencia_audit TO wins_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA wins_v2 TO wins_app;
            """
        )
    conn.commit()


def main() -> int:
    conn = connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # frequency maps
    cur.execute(
        """
        SELECT regexp_replace(telefone, '\\D', '', 'g') AS tel, COUNT(DISTINCT obra_id) AS n
          FROM decisores_obra
         WHERE excluido_em IS NULL AND NULLIF(telefone,'') IS NOT NULL
         GROUP BY 1
        """
    )
    tel_freq = {r["tel"]: int(r["n"]) for r in cur.fetchall() if r["tel"]}

    cur.execute(
        """
        SELECT lower(trim(nome)) AS nome, COUNT(DISTINCT obra_id) AS n
          FROM decisores_obra
         WHERE excluido_em IS NULL AND NULLIF(nome,'') IS NOT NULL
         GROUP BY 1
        """
    )
    nome_freq = {r["nome"]: int(r["n"]) for r in cur.fetchall() if r["nome"]}

    cur.execute(
        """
        SELECT o.id::text AS obra_id, o.classificacao_computed, o.empresa, o.cnpj,
               o.municipio, o.uf, o.status_portao
          FROM public.obras o
         WHERE o.status_portao='APROVADA' AND o.classificacao_computed='OURO'
        """
    )
    ouros = cur.fetchall()
    print(f"OURO_AUDITAR={len(ouros)}", flush=True)

    stats = Counter()
    rebaixados = []
    mantidos = []
    canal_counts = Counter()

    for o in ouros:
        oid = o["obra_id"]
        cur.execute(
            """
            SELECT nome, cargo, email, telefone, telefone_fonte, linkedin_url,
                   email_status, email_smtp_status, whatsapp_status, confianca_match, fonte
              FROM decisores_obra
             WHERE obra_id=%s AND excluido_em IS NULL
               AND (hipotese_replicacao IS NULL OR hipotese_replicacao <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO')
             ORDER BY confianca_match DESC NULLS LAST, registrado_em DESC
             LIMIT 5
            """,
            (oid,),
        )
        decs = cur.fetchall()
        # also nivel1
        cur.execute(
            """
            SELECT nivel1_nome AS nome, nivel1_cargo AS cargo, nivel1_email AS email,
                   nivel1_telefone AS telefone, NULL AS telefone_fonte, nivel1_linkedin AS linkedin_url,
                   nivel1_email_status AS email_status, NULL AS email_smtp_status,
                   NULL AS whatsapp_status, NULL AS confianca_match, 'obras.nivel1' AS fonte
              FROM public.obras WHERE id=%s AND NULLIF(nivel1_nome,'') IS NOT NULL
            """,
            (oid,),
        )
        n1 = cur.fetchone()
        if n1:
            decs = list(decs) + [n1]

        best_canal = "SEM_CANAL"
        best_meta: Dict[str, Any] = {}
        best_dec = None
        # pick best canal across decisors (prefer OURO-capable)
        rank = {c: i for i, c in enumerate([
            "EMAIL_NOMINAL_VALIDADO", "WHATSAPP_CONFIRMADO", "DIRETO_VALIDADO",
            "CELULAR_CORPORATIVO_VALIDADO", "DEPARTAMENTO_VALIDADO",
            "NAO_VALIDADO", "GERAL_EMPRESA", "EMAIL_GERAL", "INFERIDO",
            "LINKEDIN_SOMENTE", "SEM_CANAL", "INVALIDO",
        ])}
        best_rank = 999
        for d in decs:
            tel = digits(d.get("telefone"))
            nf = nome_freq.get((d.get("nome") or "").strip().lower(), 0)
            tf = tel_freq.get(tel, 0) if tel else 0
            canal, meta = classificar_canal(
                email=d.get("email"),
                email_status=d.get("email_status"),
                email_smtp=d.get("email_smtp_status"),
                telefone=d.get("telefone"),
                telefone_fonte=d.get("telefone_fonte"),
                whatsapp_status=d.get("whatsapp_status"),
                linkedin=d.get("linkedin_url"),
                tel_freq=tf,
                nome_freq=nf,
                confianca=d.get("confianca_match"),
            )
            rnk = rank.get(canal, 50)
            if rnk < best_rank:
                best_rank = rnk
                best_canal = canal
                best_meta = meta
                best_dec = d

        canal_counts[best_canal] += 1
        tem_empresa = bool((o.get("empresa") or "").strip())
        tem_decisor = bool(best_dec and (best_dec.get("nome") or "").strip())
        tem_cargo = bool(best_dec and (best_dec.get("cargo") or "").strip())

        keep_ouro = best_canal in CANAIS_OURO and tem_empresa and tem_decisor
        if keep_ouro:
            stats["ouro_mantidos"] += 1
            mantidos.append({"obra_id": oid, "canal": best_canal, "decisor": (best_dec or {}).get("nome")})
            continue

        # rebaixar para PRATA
        motivo = f"OURO sem canal acionável validado; canal_principal={best_canal}"
        if best_meta.get("motivo"):
            motivo += f" ({best_meta['motivo']})"
        just = build_justificativa("PRATA", best_canal, best_meta, tem_empresa, tem_decisor, tem_cargo)

        cur.execute(
            """
            UPDATE public.obras
               SET classificacao_computed='PRATA',
                   status_enriquecimento=COALESCE(status_enriquecimento,'PARCIAL'),
                   observacoes_enrichment=left(%s, 500)
             WHERE id=%s AND status_portao='APROVADA' AND classificacao_computed='OURO'
            """,
            (f"tier_coerencia:{motivo}", oid),
        )
        cur.execute(
            """
            INSERT INTO wins_v2.tier_coerencia_audit (
              obra_id, tier_anterior, tier_novo, motivo, telefone, tipo_telefone,
              fonte, confianca, decisor, cargo, canal_meta, regra_aplicada
            ) VALUES (%s,'OURO','PRATA',%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            """,
            (
                oid,
                motivo[:500],
                best_meta.get("telefone") or digits((best_dec or {}).get("telefone")),
                best_canal,
                (best_dec or {}).get("fonte"),
                just["confianca"],
                (best_dec or {}).get("nome"),
                (best_dec or {}).get("cargo"),
                json.dumps({"canal": best_canal, "meta": best_meta, "justificativa": just}, ensure_ascii=False, default=str),
                "TIER_COERENCIA_CANAL_ACIONAVEL_V1",
            ),
        )
        stats["ouro_rebaixados"] += 1
        rebaixados.append({
            "obra_id": oid,
            "canal": best_canal,
            "telefone": best_meta.get("telefone"),
            "decisor": (best_dec or {}).get("nome"),
            "motivo": motivo,
        })
        conn.commit()

    # pilot explicit
    pilot = "b72d3db9-875b-4ec4-8678-4f574acecb93"
    cur.execute(
        """
        SELECT o.classificacao_computed, d.telefone, d.fonte, d.nome, d.cargo, d.linkedin_url, d.email
          FROM public.obras o
          LEFT JOIN decisores_obra d ON d.obra_id=o.id AND d.excluido_em IS NULL
         WHERE o.id=%s
         ORDER BY d.confianca_match DESC NULLS LAST
         LIMIT 1
        """,
        (pilot,),
    )
    pilot_row = cur.fetchone()

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ouro_auditado": len(ouros),
        "ouro_mantidos": stats["ouro_mantidos"],
        "ouro_rebaixados": stats["ouro_rebaixados"],
        "canais": dict(canal_counts),
        "piloto": dict(pilot_row) if pilot_row else None,
        "rebaixados_sample": rebaixados[:20],
    }
    # final counts
    cur.execute(
        """
        SELECT classificacao_computed, COUNT(*)
          FROM public.obras WHERE status_portao='APROVADA'
         GROUP BY 1 ORDER BY 2 DESC
        """
    )
    out["distribuicao_aprovada"] = {r["classificacao_computed"]: r["count"] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) n FROM wins_v2.tier_coerencia_audit")
    out["audit_rows"] = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM public.obras")
    out["v1_total"] = cur.fetchone()["n"]

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    Path = __import__("pathlib").Path
    out_dir = Path(os.environ.get("TIER_OUT", "/tmp/tier_coerencia"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "AUDITORIA_OURO.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
