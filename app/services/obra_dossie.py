#!/usr/bin/env python3
"""Dossiê comercial da obra — seleção de campos úteis ao fornecedor.

Não inventa dados. Não expõe 288 campos crus. Somente status_portao=APROVADA.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


def _email_generico(em: str) -> bool:
    local = (em or "").split("@")[0].lower()
    return local in {
        "contato", "sac", "ouvidoria", "info", "informacoes", "noreply", "no-reply",
        "admin", "suporte", "financeiro", "rh", "atendimento", "comercial", "vendas",
        "secretaria", "protocolo",
    }


def classificar_tipo_telefone(telefone, telefone_fonte=None, whatsapp_status=None, tel_freq=0, nome_freq=0, confianca=None):
    tel = re.sub(r"\D", "", str(telefone or ""))
    if not tel or len(tel) < 10:
        return "INVALIDO", {}
    wa = (whatsapp_status or "").lower()
    tf = (telefone_fonte or "").lower()
    if wa in ("confirmado", "confirmed", "ativo", "sim", "ok", "validado"):
        return "WHATSAPP_CONFIRMADO", {"telefone": tel}
    if tel_freq >= 10:
        return "GERAL_EMPRESA", {"telefone": tel, "freq_obras": tel_freq}
    is_cel = len(tel) == 11 and tel[2] == "9"
    if "direto" in tf or "ramal" in tf:
        return ("CELULAR_CORPORATIVO_VALIDADO" if is_cel else "DIRETO_VALIDADO"), {"telefone": tel, "telefone_fonte": tf}
    if "departamento" in tf or "setor" in tf:
        return "DEPARTAMENTO_VALIDADO", {"telefone": tel}
    if nome_freq >= 30:
        return "INFERIDO", {"telefone": tel, "nome_freq": nome_freq}
    return "NAO_VALIDADO", {"telefone": tel, "freq_obras": tel_freq}


CANAIS_OURO = {
    "DIRETO_VALIDADO", "CELULAR_CORPORATIVO_VALIDADO", "WHATSAPP_CONFIRMADO",
    "EMAIL_NOMINAL_VALIDADO", "DEPARTAMENTO_VALIDADO",
}


def build_tier_justificativa(tier, canal_tipo, canal_meta, tem_empresa, tem_decisor, tem_cargo):
    atendidos = ["Obra confirmada (Portão APROVADA)"]
    pendentes = []
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
    labels = {
        "DIRETO_VALIDADO": "Telefone direto validado",
        "CELULAR_CORPORATIVO_VALIDADO": "Celular corporativo validado",
        "WHATSAPP_CONFIRMADO": "WhatsApp confirmado",
        "EMAIL_NOMINAL_VALIDADO": "E-mail corporativo nominal validado",
        "DEPARTAMENTO_VALIDADO": "Contato de departamento validado",
        "GERAL_EMPRESA": "Telefone geral da organização",
        "EMAIL_GERAL": "E-mail geral",
        "LINKEDIN_SOMENTE": "LinkedIn (perfil profissional)",
        "INFERIDO": "Contato inferido / não validado",
        "NAO_VALIDADO": "Telefone sem validação plena",
        "SEM_CANAL": "Sem canal de contato",
    }
    cl = labels.get(canal_tipo, canal_tipo or "—")
    if canal_tipo in CANAIS_OURO:
        atendidos.append(cl)
    else:
        if canal_tipo and canal_tipo != "SEM_CANAL":
            pendentes.append(cl + " — não basta para OURO")
        else:
            pendentes.append("Canal acionável validado")
        if canal_tipo == "LINKEDIN_SOMENTE":
            pendentes.append("LinkedIn não é canal acionável validado")
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
        "canal_principal": {"tipo": canal_tipo, "label": cl, **(canal_meta or {})},
        "confianca": 0.95 if canal_tipo in CANAIS_OURO else 0.7 if tem_decisor else 0.5,
    }



def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _money(v: Any) -> Optional[Dict[str, Any]]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 1_000_000_000:
        label = f"R$ {n/1e9:.2f}".replace(".", ",") + " bi"
    elif n >= 1_000_000:
        label = f"R$ {n/1e6:.1f}".replace(".", ",") + " mi"
    else:
        label = "R$ " + f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return {"valor": n, "formatado": label, "moeda": "BRL"}


def _dt(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    t = str(v)[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", t):
        return t
    return None


def _fmt_br_date(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        y, m, d = iso[:10].split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return iso


def _titulo_canonico(nome: Optional[str], desc: Optional[str]) -> str:
    n = (nome or "").strip()
    d = (desc or "").strip()
    # "JOAO SUSSUMU HIRATA - CLIMATIZAÇÃO" → legível
    if " - " in n:
        partes = [p.strip() for p in n.split(" - ", 1)]
        if len(partes) == 2 and len(partes[0]) < 80:
            # se parece NOME - ESCOPO
            escopo = partes[1].title() if partes[1].isupper() or partes[1] == partes[1].upper() else partes[1]
            local = partes[0].title() if partes[0].isupper() else partes[0]
            # Prefer "Climatização — João Sussumu Hirata" when desc mentions school
            if any(k in (d or "").lower() for k in ("escola", "educa", "aluno", "pedag")):
                return f"{escopo} da Escola {local}"
            return f"{escopo} — {local}"
    if n.isupper() and len(n) > 8:
        return n.title()
    return n or "Obra sem título"


def _tier_label(tier: Optional[str]) -> Dict[str, str]:
    t = (tier or "").upper()
    return {
        "OURO": {
            "tier": "OURO",
            "label": "Pronta para contato",
            "justificativa": "Decisor e contato acionável identificados",
        },
        "PRATA": {
            "tier": "PRATA",
            "label": "Decisor parcial",
            "justificativa": "Decisor identificado; contato parcial ou em validação",
        },
        "BRONZE": {
            "tier": "BRONZE",
            "label": "Em validação comercial",
            "justificativa": "Empresa identificada; enriquecimento comercial em validação",
        },
        "PIPELINE": {
            "tier": "PIPELINE",
            "label": "Aguardando enriquecimento",
            "justificativa": "Obra confirmada; dados comerciais ainda insuficientes",
        },
    }.get(t, {"tier": t or "—", "label": "—", "justificativa": "—"})


def _fase_label(fase: Optional[str], fase_real: Optional[str]) -> Optional[str]:
    f = (fase_real or fase or "").upper()
    mapa = {
        "PLANEJAMENTO": "Planejamento",
        "LICENCIAMENTO": "Licenciamento",
        "LICITACAO": "Licitação",
        "LICITACAO_ABERTA": "Licitação aberta",
        "CONTRATACAO": "Contratação",
        "EM_EXECUCAO": "Em execução",
        "PARALISADA": "Paralisada",
        "CONCLUIDA": "Concluída",
        "DESCONHECIDA": None,
        "PROJETO": "Projeto",
        "LICENCA_PREVIA": "Licença prévia",
        "LICENCA_INSTALACAO": "Licença de instalação",
    }
    return mapa.get(f, f.title() if f else None)


def _contato_status(email, telefone, linkedin, email_status=None) -> Dict[str, Any]:
    em = _s(email)
    tel = _s(telefone)
    li = _s(linkedin)
    est = (_s(email_status) or "").lower()
    validado = False
    if em and est in ("valido", "valid", "ok", "smtp_ok"):
        validado = True
    if tel and len(re.sub(r"\D", "", tel)) >= 10 and est in ("valido", "valid", "ok", ""):
        # telefone com dígitos + sem status inválido
        if est not in ("invalid", "invalido"):
            if tel:
                validado = validado or (est in ("valido", "valid", "ok"))
    tipos = []
    if em:
        tipos.append("email")
    if tel:
        tipos.append("telefone")
    if li:
        tipos.append("linkedin")
    if validado and (em or tel):
        status = "validado"
    elif em or tel:
        status = "parcial"
    elif li:
        status = "parcial"  # linkedin alone = partial, not fully validated acionável
    else:
        status = "nao_validado"
    return {
        "status": status,
        "canais": tipos,
        "acionavel_validado": bool(validado and (em or tel)),
    }


ESCOPO_KEYWORDS = [
    ("climatização", "serviço", "confirmado"),
    ("ar condicionado", "equipamento", "confirmado"),
    ("infraestrutura elétrica", "serviço", "confirmado"),
    ("instalação elétrica", "serviço", "confirmado"),
    ("instalação de aparelhos", "serviço", "confirmado"),
    ("instalação de ar", "serviço", "confirmado"),
    ("construção", "serviço", "confirmado"),
    ("pavimentação", "serviço", "confirmado"),
    ("saneamento", "infraestrutura", "confirmado"),
    ("subestação", "equipamento", "confirmado"),
]

PROVAVEL_KEYWORDS = [
    ("quadro elétrico", "equipamento", "provável"),
    ("cabeamento", "material", "provável"),
    ("duto", "material", "provável"),
    ("manutenção predial", "serviço", "potencial"),
    ("automação", "serviço", "potencial"),
]


def _extract_escopo(texto: str) -> tuple:
    t = (texto or "").lower()
    conf, prov = [], []
    seen = set()
    for nome, tipo, flag in ESCOPO_KEYWORDS:
        if nome in t and nome not in seen:
            seen.add(nome)
            conf.append({
                "nome": nome.title(),
                "tipo": tipo,
                "status": "confirmado",
                "confianca": 0.9,
                "evidencia": "objeto/descrição da obra",
            })
    for nome, tipo, flag in PROVAVEL_KEYWORDS:
        if nome in t and nome not in seen:
            # only if related base exists
            if any(base in t for base in ("elétr", "eletr", "clima", "ar condicionado", "instala")):
                seen.add(nome)
                prov.append({
                    "nome": nome.title(),
                    "tipo": tipo,
                    "status": flag,
                    "confianca": 0.55,
                    "evidencia": "inferido do escopo; requer validação",
                })
    return conf[:12], prov[:8]


def build_dossie(oid: str, conn, *, filtrar_obra_fn=None, u=None, plano: str = "GRATUITO",
                 desbloqueada: bool = False) -> Dict[str, Any]:
    from psycopg2.extras import RealDictCursor

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT o.*,
              EXISTS (
                SELECT 1 FROM decisores_obra d
                WHERE d.obra_id=o.id AND d.excluido_em IS NULL
              ) AS tem_decisor_externo
            FROM public.obras o
            WHERE o.id = %s
            """,
            (oid,),
        )
        obra = cur.fetchone()
        if not obra:
            return {"erro": "nao_encontrada"}
        if (obra.get("status_portao") or "") != "APROVADA":
            return {"erro": "nao_aprovada", "status_portao": obra.get("status_portao")}

        # V2 captura
        cur.execute(
            """
            SELECT id::text, id_externo, capturado_em, campos_canonicos, url_origem
              FROM wins_v2.capturas_brutas
             WHERE v1_obra_id = %s
             ORDER BY capturado_em DESC NULLS LAST
             LIMIT 1
            """,
            (oid,),
        )
        cap = cur.fetchone()

        # valores_normalizados (if any)
        valores_v2 = []
        if cap:
            cur.execute(
                """
                SELECT vn.valor_normalizado, vn.valor_original, vn.tipo_origem, vn.confianca,
                       cc.id AS campo
                  FROM wins_v2.valores_normalizados vn
                  JOIN wins_v2.campos_canonicos cc ON cc.id = vn.campo_canonico_id
                 WHERE vn.captura_bruta_id = %s
                   AND cc.id IN ('valor_capex','valor_referencia','valor_financiamento','titulo','municipio_obra')
                 LIMIT 30
                """,
                (cap["id"],),
            )
            valores_v2 = [dict(r) for r in cur.fetchall()]

        # decisores
        cur.execute(
            """
            SELECT id::text, nome, cargo, email, telefone, linkedin_url, fonte,
                   confianca_match, email_status, tipo_cargo, qualidade_lead, registrado_em
              FROM decisores_obra
             WHERE obra_id=%s AND excluido_em IS NULL
               AND (hipotese_replicacao IS NULL OR hipotese_replicacao <> 'REPLICADO_PROVAVEL_FALSO_POSITIVO')
             ORDER BY confianca_match DESC NULLS LAST, registrado_em DESC
             LIMIT 20
            """,
            (oid,),
        )
        decisores_rows = [dict(r) for r in cur.fetchall()]

        # entidades V2
        entidades = []
        if cap:
            cur.execute(
                """
                SELECT e.cnpj, e.nome, ce.papel, ce.confianca
                  FROM wins_v2.captura_entidades ce
                  JOIN wins_v2.entidades e ON e.id = ce.entidade_id
                 WHERE ce.captura_bruta_id = %s AND ce.ativo IS DISTINCT FROM false
                 LIMIT 20
                """,
                (cap["id"],),
            )
            entidades = [dict(r) for r in cur.fetchall()]

        # matches summary (limited)
        cur.execute(
            """
            SELECT c.nome AS categoria, c.codigo, COUNT(*) AS qtd,
                   ROUND(AVG(m.score)::numeric, 1) AS score_medio
              FROM matches_obra_prestador m
              JOIN categorias_servico c ON c.id = m.categoria_id
             WHERE m.obra_id = %s AND m.score >= 50
             GROUP BY c.nome, c.codigo, c.ordem
             ORDER BY COUNT(*) DESC, c.ordem
             LIMIT 12
            """,
            (oid,),
        )
        cats = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT m.cnpj) AS empresas,
                   COUNT(DISTINCT m.categoria_id) AS categorias
              FROM matches_obra_prestador m
             WHERE m.obra_id = %s AND m.score >= 50
            """,
            (oid,),
        )
        match_tot = dict(cur.fetchone() or {})

        # top matches sample (not labeled as executoras)
        cur.execute(
            """
            SELECT m.score, m.cnpj, c.nome AS categoria,
                   COALESCE(NULLIF(f.nome_fantasia,''), f.razao_social) AS nome,
                   f.municipio_nome, f.uf
              FROM matches_obra_prestador m
              JOIN categorias_servico c ON c.id = m.categoria_id
              LEFT JOIN fornecedores f ON f.cnpj = m.cnpj
             WHERE m.obra_id = %s AND m.score >= 60
             ORDER BY m.score DESC
             LIMIT 8
            """,
            (oid,),
        )
        top_matches = [dict(r) for r in cur.fetchall()]

        # lookup empresa
        cnpj = re.sub(r"\D", "", str(obra.get("cnpj") or ""))
        lookup = None
        if len(cnpj) == 14:
            cur.execute(
                """
                SELECT razao_social, nome_fantasia, municipio, uf, situacao
                  FROM wins_v2.entidades_lookup WHERE cnpj_normalizado=%s
                """,
                (cnpj,),
            )
            lookup = cur.fetchone()

    obra = dict(obra)
    desc = _s(obra.get("descricao_publica")) or _s(obra.get("descricao")) or ""
    titulo = _titulo_canonico(_s(obra.get("nome")), desc)
    tier_info = _tier_label(obra.get("classificacao_computed"))
    valor = _money(obra.get("valor_estimado"))
    valor_fmt = _s(obra.get("valor_formatado"))
    if valor and not valor_fmt:
        valor_fmt = valor["formatado"]
    elif valor_fmt in ("R$ 0", "R$ 0,00", "0", "A definir", "a definir"):
        valor_fmt = valor["formatado"] if valor else None

    mun = _s(obra.get("municipio"))
    uf = _s(obra.get("uf"))
    local_txt = None
    if mun and uf:
        local_txt = f"{mun}/{uf}"
    elif uf:
        local_txt = uf
    elif mun:
        local_txt = mun

    fase_lbl = _fase_label(obra.get("fase"), obra.get("fase_real_obra"))
    criado = _dt(obra.get("criado_em"))
    pub = _dt(obra.get("data_publicacao")) or _dt(obra.get("data_anuncio"))

    # --- cabecalho ---
    cabecalho = {
        "obra_id": str(obra["id"]),
        "titulo": titulo,
        "titulo_original": _s(obra.get("nome")),
        "tier": tier_info["tier"],
        "tier_label": tier_info["label"],
        "tier_justificativa": None,
        "setor": _s(obra.get("setor")),
        "localizacao_resumo": local_txt,
        "municipio": mun,
        "uf": uf,
        "fase": fase_lbl,
        "fase_codigo": _s(obra.get("fase")),
        "valor_principal": valor_fmt or "Valor ainda não identificado",
        "valor_numero": valor["valor"] if valor else None,
        "tipo_valor": "Valor estimado" if valor else None,
        "fonte_principal": _s(obra.get("fonte")),
        "fonte_tipo": _s(obra.get("fonte_tipo")),
        "url_fonte": _s(obra.get("url_fonte")),
        "atualizado_em": _fmt_br_date(criado),
        "portao": "Obra confirmada" if obra.get("status_portao") == "APROVADA" else None,
    }

    # --- resumo ---
    partes = []
    if desc:
        # first sentence-ish
        s0 = re.split(r"[.\n]", desc)[0].strip()
        if s0:
            partes.append(s0[:280] + ("…" if len(s0) > 280 else ""))
    if local_txt:
        partes.append(f"Localização: {local_txt}.")
    if fase_lbl:
        partes.append(f"Fase: {fase_lbl}.")
    if valor_fmt:
        partes.append(f"Valor de referência: {valor_fmt}.")
    if _s(obra.get("empresa")):
        partes.append(f"Entidade principal: {_s(obra.get('empresa'))}.")
    resumo = {
        "texto": " ".join(partes)[:900] if partes else "Resumo ainda em consolidação a partir do objeto original.",
        "baseado_em": ["descricao", "localizacao", "fase", "valor", "empresa"],
    }

    # --- escopo ---
    conf, prov = _extract_escopo(f"{obra.get('nome') or ''} {desc}")
    if not conf and desc:
        conf.append({
            "nome": "Objeto da contratação",
            "tipo": "objeto",
            "status": "confirmado",
            "confianca": 0.85,
            "evidencia": "descrição original da obra",
            "detalhe": desc[:400],
        })

    # categorias fornecimento from matches
    blob_escopo = f"{obra.get('nome') or ''} {desc}".lower()
    related_tokens = ("clima", "ar cond", "eletr", "hvac", "instal", "refriger", "ventil",
                      "predial", "ar-cond", "condicion", "eletrotec", "cabeam", "ilum")
    categorias = []
    related, other = [], []
    for c in cats:
        if not c.get("categoria"):
            continue
        nome_c = (c.get("categoria") or "").lower()
        off = any(x in nome_c for x in ("offshore", "ferrovi", "portuar", "dragagem", "miner", "petrole", "oleodut", "arte especial", "obras de arte", "sondagem", "barragen", "rodovia", "aeroporto", "caldeir", "tubulac", "icamento", "montagem industrial"))
        if off:
            continue
        rel = any(tok in nome_c for tok in related_tokens) or any(tok in blob_escopo and tok in nome_c for tok in related_tokens)
        # soft: consultoria/engenharia always secondary for HVAC school
        item = {
            "nome": c["categoria"],
            "codigo": c.get("codigo"),
            "aderencia": "alta" if rel else "média",
            "score_medio": float(c["score_medio"] or 0),
            "fornecedores_compativeis": int(c["qtd"] or 0),
            "origem": "match_cnae_obra",
            "motivo": ("Categoria alinhada ao escopo de climatização/elétrica" if rel
                       else "Categoria genérica de engenharia — validar aderência ao objeto"),
        }
        (related if rel else other).append(item)
    categorias = (related + other)[:8]

    # valores
    valores = []
    if valor:
        valores.append({
            "rotulo": "Valor mestre",
            "valor_formatado": valor_fmt,
            "valor": valor["valor"],
            "tipo": "valor_estimado",
            "moeda": "BRL",
            "fonte": _s(obra.get("fonte")),
            "confianca": "alta" if obra.get("fonte_tipo") == "OFICIAL" else "média",
            "data_referencia": _fmt_br_date(pub or criado),
        })
    for vv in valores_v2:
        if vv.get("campo") in ("valor_capex", "valor_referencia", "valor_financiamento"):
            m = _money(vv.get("valor_normalizado"))
            if m:
                valores.append({
                    "rotulo": vv["campo"].replace("_", " ").title(),
                    "valor_formatado": m["formatado"],
                    "valor": m["valor"],
                    "tipo": vv["campo"],
                    "fonte": "base_mestre_v2",
                    "confianca": str(vv.get("confianca") or "média"),
                })

    # localizacao — nunca inferir município da obra pela sede
    localizacao = {
        "obra": {
            "municipio": mun or None,
            "uf": uf or None,
            "endereco": None,
            "resumo": (f"{mun}/{uf}" if mun and uf else (f"UF: {uf}" if uf else "Local da obra ainda não identificado")),
            "municipio_status": "identificado" if mun else "ainda_nao_identificado",
            "endereco_status": "ainda_nao_identificado",
        },
        "unidade_beneficiada": None,
        "contratante": {
            "nome": _s(obra.get("empresa")),
            "municipio": _s((lookup or {}).get("municipio")) if lookup else None,
            "uf": _s((lookup or {}).get("uf")) if lookup else None,
            "tipo": "sede_cadastral",
            "nota": "Sede da contratante — não é o local da obra",
        } if (lookup or _s(obra.get("empresa"))) else None,
        "confianca": {
            "obra_municipio": "alta" if mun else "baixa",
            "obra_uf": "alta" if uf else "baixa",
            "sede_nao_e_local_obra": True,
        },
    }

    # timeline
    timeline = []
    if criado:
        timeline.append({"evento": "Primeira detecção no WiNS Hub", "data": _fmt_br_date(criado), "iso": criado})
    if pub:
        timeline.append({"evento": "Publicação / referência", "data": _fmt_br_date(pub), "iso": pub})
    if fase_lbl:
        timeline.append({"evento": f"Fase atual: {fase_lbl}", "data": None, "iso": None})
    if cap and cap.get("capturado_em"):
        timeline.append({
            "evento": "Captura na Base Mestre",
            "data": _fmt_br_date(_dt(cap["capturado_em"])),
            "iso": _dt(cap["capturado_em"]),
        })

    # entidades
    ents_out = []
    if _s(obra.get("empresa")):
        papel = "CONTRATANTE" if (obra.get("fonte") or "").startswith("pncp") or "obrasgov" in (obra.get("fonte") or "") else "ENTIDADE_PRINCIPAL"
        ents_out.append({
            "nome": _s(obra.get("empresa")),
            "cnpj": _s(obra.get("cnpj")),
            "papel": papel,
            "papel_label": "Contratante / órgão" if papel == "CONTRATANTE" else "Entidade principal",
            "municipio": (lookup or {}).get("municipio") if lookup else None,
            "situacao": (lookup or {}).get("situacao") if lookup else None,
            "fonte": _s(obra.get("fonte")),
            "confianca": "alta",
        })
    if _s(obra.get("empresa_executora")):
        ents_out.append({
            "nome": _s(obra.get("empresa_executora")),
            "cnpj": _s(obra.get("cnpj_executora")),
            "papel": "EXECUTORA",
            "papel_label": "Executora confirmada",
            "fonte": _s(obra.get("executora_fonte")) or "obra",
            "confianca": "alta",
        })
    cnpj_principal = re.sub(r"\D", "", str(obra.get("cnpj") or ""))
    for e in entidades:
        if not e.get("nome"):
            continue
        papel = e.get("papel") or "NAO_CLASSIFICADO"
        ec = re.sub(r"\D", "", str(e.get("cnpj") or ""))
        # evita duplicar contratante como executora sem evidência própria
        if papel == "EXECUTORA" and ec and ec == cnpj_principal and not _s(obra.get("empresa_executora")):
            continue
        if any((x.get("cnpj") or "") == (e.get("cnpj") or "") and x.get("papel") == papel for x in ents_out):
            continue
        ents_out.append({
            "nome": e.get("nome"),
            "cnpj": e.get("cnpj"),
            "papel": papel,
            "papel_label": (papel or "Entidade").replace("_", " ").title(),
            "fonte": "base_mestre_v2",
            "confianca": str(e.get("confianca") or "média"),
        })

    # decisores
    pode_contato = bool(desbloqueada or (plano and plano.upper() not in ("", "GRATUITO")))
    decisores_out = []
    for d in decisores_rows:
        st = _contato_status(d.get("email"), d.get("telefone"), d.get("linkedin_url"), d.get("email_status"))
        item = {
            "nome": _s(d.get("nome")),
            "cargo": _s(d.get("cargo")),
            "empresa": _s(obra.get("empresa")),
            "papel": "Decisor comercial",
            "fonte": _s(d.get("fonte")),
            "confianca": d.get("confianca_match"),
            "qualidade_lead": d.get("qualidade_lead"),
            "contato_status": st["status"],
            "acionavel_validado": st["acionavel_validado"],
            "canais": st["canais"],
            "linkedin": _s(d.get("linkedin_url")),
        }
        if pode_contato:
            item["email"] = _s(d.get("email"))
            item["telefone"] = _s(d.get("telefone"))
        else:
            item["email"] = None
            item["telefone"] = None
            item["contato_mascarado"] = True
        decisores_out.append(item)

    # nivel1 fallback
    if not decisores_out and _s(obra.get("nivel1_nome")):
        st = _contato_status(
            obra.get("nivel1_email"), obra.get("nivel1_telefone"),
            obra.get("nivel1_linkedin"), obra.get("nivel1_email_status"),
        )
        decisores_out.append({
            "nome": _s(obra.get("nivel1_nome")),
            "cargo": _s(obra.get("nivel1_cargo")),
            "empresa": _s(obra.get("empresa")),
            "papel": "Decisor comercial",
            "fonte": _s(obra.get("nivel1_origem_enrichment")) or "obra",
            "contato_status": st["status"],
            "acionavel_validado": st["acionavel_validado"],
            "canais": st["canais"],
            "linkedin": _s(obra.get("nivel1_linkedin")),
            "email": _s(obra.get("nivel1_email")) if pode_contato else None,
            "telefone": _s(obra.get("nivel1_telefone")) if pode_contato else None,
            "contato_mascarado": not pode_contato,
        })

    # refine tier justificativa from actual contacts
    if decisores_out:
        any_val = any(d.get("acionavel_validado") for d in decisores_out)
        any_partial = any(d.get("contato_status") in ("parcial", "validado") for d in decisores_out)
        if tier_info["tier"] == "OURO" and not any_val:
            # phone/linkedin partial
            if any(d.get("linkedin") or d.get("telefone") for d in decisores_out):
                cabecalho["tier_justificativa"] = (
                    "Decisor identificado com contato parcial (ex.: telefone/LinkedIn); "
                    "validação completa de e-mail ainda pendente"
                )
            else:
                cabecalho["tier_justificativa"] = "Decisor identificado; reforçar validação de contato"
        elif tier_info["tier"] == "PRATA" and any_partial:
            cabecalho["tier_justificativa"] = "Decisor identificado; contato parcial ou em validação"


    # --- canal principal + justificativa estruturada do tier ---
    tel_freq_map, nome_freq_map = {}, {}
    with conn.cursor() as cur2:
        tels = list({re.sub(r"\D", "", str(d.get("telefone") or "")) for d in decisores_rows if d.get("telefone")})
        tels = [x for x in tels if len(x) >= 10]
        if tels:
            cur2.execute(
                "SELECT regexp_replace(telefone, '\\D', '', 'g') t, COUNT(DISTINCT obra_id) n FROM decisores_obra "
                "WHERE excluido_em IS NULL AND regexp_replace(telefone, '\\D', '', 'g') = ANY(%s) GROUP BY 1",
                (tels,),
            )
            tel_freq_map = {r[0]: r[1] for r in cur2.fetchall()}
        nomes = list({(d.get("nome") or "").strip().lower() for d in decisores_rows if d.get("nome")})
        if nomes:
            cur2.execute(
                "SELECT lower(trim(nome)) n, COUNT(DISTINCT obra_id) c FROM decisores_obra "
                "WHERE excluido_em IS NULL AND lower(trim(nome)) = ANY(%s) GROUP BY 1",
                (nomes,),
            )
            nome_freq_map = {r[0]: r[1] for r in cur2.fetchall()}

    best_canal, best_meta = "SEM_CANAL", {}
    rank = {c: i for i, c in enumerate([
        "EMAIL_NOMINAL_VALIDADO", "WHATSAPP_CONFIRMADO", "DIRETO_VALIDADO", "CELULAR_CORPORATIVO_VALIDADO",
        "DEPARTAMENTO_VALIDADO", "NAO_VALIDADO", "GERAL_EMPRESA", "EMAIL_GERAL", "INFERIDO", "LINKEDIN_SOMENTE", "SEM_CANAL",
    ])}
    br = 999
    for d in decisores_rows:
        tel = re.sub(r"\D", "", str(d.get("telefone") or ""))
        em = (d.get("email") or "").strip()
        est = (d.get("email_status") or "").lower()
        smtp = (str(d.get("email_smtp_status") or "")).lower()
        if em and "@" in em and not _email_generico(em) and (est in ("valido", "valid", "ok", "smtp_ok") or smtp in ("valid", "valido", "ok")):
            canal, meta = "EMAIL_NOMINAL_VALIDADO", {"email": em}
        else:
            canal, meta = classificar_tipo_telefone(
                d.get("telefone"), d.get("telefone_fonte"), d.get("whatsapp_status"),
                tel_freq_map.get(tel, 0), nome_freq_map.get((d.get("nome") or "").strip().lower(), 0),
                d.get("confianca_match"),
            )
            if canal in ("SEM_CANAL", "INVALIDO"):
                if d.get("linkedin_url"):
                    canal, meta = "LINKEDIN_SOMENTE", {"linkedin": d.get("linkedin_url")}
                elif em:
                    canal, meta = ("EMAIL_GERAL" if _email_generico(em) else "INFERIDO"), {"email": em}
        if rank.get(canal, 50) < br:
            br, best_canal, best_meta = rank.get(canal, 50), canal, meta
        for dout in decisores_out:
            if dout.get("nome") == d.get("nome"):
                ct, cm = canal, meta
                dout["tipo_telefone"] = ct
                dout["tipo_telefone_label"] = {
                    "DIRETO_VALIDADO": "Telefone direto validado",
                    "CELULAR_CORPORATIVO_VALIDADO": "Celular corporativo validado",
                    "WHATSAPP_CONFIRMADO": "WhatsApp confirmado",
                    "GERAL_EMPRESA": "Telefone geral da organização",
                    "NAO_VALIDADO": "Telefone não validado",
                    "INFERIDO": "Contato inferido",
                    "LINKEDIN_SOMENTE": "Perfil LinkedIn (não é canal acionável validado)",
                    "EMAIL_NOMINAL_VALIDADO": "E-mail nominal validado",
                    "DEPARTAMENTO_VALIDADO": "Departamento validado",
                    "EMAIL_GERAL": "E-mail geral",
                    "SEM_CANAL": "Sem telefone",
                    "INVALIDO": "Inválido",
                }.get(ct, ct)
                dout["acionavel_validado"] = ct in CANAIS_OURO
                dout["contato_status"] = "validado" if ct in CANAIS_OURO else ("parcial" if ct not in ("SEM_CANAL", "INVALIDO") else "nao_validado")
                if ct == "LINKEDIN_SOMENTE":
                    dout["linkedin_nota"] = "Perfil profissional — não conta sozinho como contato acionável validado"
                if ct == "GERAL_EMPRESA":
                    dout["telefone_nota"] = "Telefone geral/reutilizado — não é contato direto do decisor"

    tem_empresa = bool(_s(obra.get("empresa")))
    tem_decisor = bool(decisores_out)
    tem_cargo = any(bool(d.get("cargo")) for d in decisores_out)
    tier_just = build_tier_justificativa(tier_info["tier"], best_canal, best_meta, tem_empresa, tem_decisor, tem_cargo)
    cabecalho["tier_justificativa"] = tier_just
    cabecalho["tier_label"] = {
        "OURO": "Pronta para contato",
        "PRATA": "Decisor identificado · contato parcial",
        "BRONZE": "Empresa identificada · em validação",
        "PIPELINE": "Aguardando enriquecimento",
    }.get(tier_info["tier"], tier_info.get("label"))

    # documentos — portal geral ≠ documento específico
    fontes_origem = []
    documentos = []
    url_f = _s(obra.get("url_fonte"))
    if url_f:
        generico = bool(re.search(r"obrasgov\.sistema\.gov\.br/?$", url_f)) or url_f.rstrip("/").count("/") <= 3
        if generico:
            fontes_origem.append({
                "tipo": "portal_geral",
                "titulo": f"Fonte de origem: {_s(obra.get('fonte')) or 'Portal'}",
                "fonte": _s(obra.get("fonte")),
                "url": url_f,
                "nota": "Portal geral — não é edital/contrato/documento específico da obra",
            })
            documentos.append({
                "tipo": "pagina_especifica",
                "titulo": "Página específica da obra",
                "status": "ainda_nao_localizada",
                "url": None,
            })
            documentos.append({
                "tipo": "documento_especifico",
                "titulo": "Documento oficial específico",
                "status": "ainda_nao_localizado",
                "url": None,
            })
        else:
            documentos.append({
                "tipo": "pagina_especifica",
                "titulo": "Página específica da obra",
                "fonte": _s(obra.get("fonte")),
                "url": url_f,
                "data": _fmt_br_date(pub or criado),
            })
    else:
        documentos.append({
            "tipo": "documento_especifico",
            "titulo": "Documento oficial específico",
            "status": "ainda_nao_localizado",
            "url": None,
        })

    # qualidade
    pendencias = []
    if not mun:
        pendencias.append("município da obra")
    if not valor:
        pendencias.append("valor confiável")
    if not decisores_out:
        pendencias.append("decisor comercial")
    elif not any(d.get("acionavel_validado") for d in decisores_out):
        pendencias.append("contato acionável validado")
    qualidade = {
        "portao": "Obra confirmada",
        "status_portao": "APROVADA",
        "tier": tier_info["tier"],
        "tier_label": tier_info["label"],
        "fontes": 1 + (1 if cap else 0),
        "campos_pendentes": pendencias,
        "ultima_verificacao": _fmt_br_date(criado),
        "captura_v2": bool(cap),
    }

    # matches block — correct labels
    matches = {
        "resumo": {
            "fornecedores_compativeis": int(match_tot.get("empresas") or 0),
            "matches_calculados": int(match_tot.get("total") or 0),
            "categorias": int(match_tot.get("categorias") or 0),
        },
        "executoras_confirmadas": [
            e for e in ents_out if e.get("papel") == "EXECUTORA"
        ],
        "categorias_fornecimento": categorias,
        "matches_recomendados": [
            {
                "empresa": _s(m.get("nome")) or "Fornecedor",
                "cnpj": _s(m.get("cnpj")),
                "categoria": _s(m.get("categoria")),
                "localizacao": (
                    f"{m.get('municipio_nome')}/{m.get('uf')}"
                    if m.get("municipio_nome") and m.get("uf")
                    else (_s(m.get("uf")) or None)
                ),
                "score": float(m["score"]) if m.get("score") is not None else None,
                "motivo": "Compatibilidade CNAE/escopo com a obra",
                "tipo": "fornecedor_compativel",
            }
            for m in top_matches
            if m.get("nome") or m.get("cnpj")
        ],
        "nota": (
            "Fornecedores compatíveis não são executoras da obra. "
            "São empresas com potencial de fornecimento calculado por aderência."
        ),
    }

    return {
        "cabecalho": {k: v for k, v in cabecalho.items() if v not in (None, "", [])},
        "resumo": resumo,
        "escopo_confirmado": conf,
        "oportunidades_provaveis": prov,
        "categorias_fornecimento": categorias,
        "valores": valores,
        "localizacao": localizacao,
        "timeline": timeline,
        "entidades": ents_out,
        "decisores": decisores_out,
        "documentos": documentos,
        "fontes_origem": fontes_origem if "fontes_origem" in locals() else [],
        "qualidade": qualidade,
        "matches": matches,
        "meta": {
            "obra_id": str(obra["id"]),
            "status_portao": "APROVADA",
            "campos_expostos": "dossie_comercial_v1",
            "nao_inclui": ["288_campos_crus", "hashes", "ids_internos", "json_pipeline"],
        },
    }
