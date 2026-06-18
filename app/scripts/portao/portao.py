#!/usr/bin/env python3
"""
portao.py — Portão de entrada de obras (estágios 2–4). NÃO insere; decide + enriquece.

Contrato:  avaliar(obra: dict, fonte_meta: dict, conn) -> Veredito(dict)
  Estágio 2  PORTÃO-DURO  : fonte aposentada / notícia<4campos / não-é-obra /
                            setor OUTRO / CNPJ inválido-guarda-chuva / duplicata
  Estágio 3  ENRIQUECE    : FASE 0 interna (fornecedores -> decisores_preservados ->
                            empresa_dominios) e SÓ o que sobrar marca externo_pendente.
                            Hunter NUNCA inline (enfileira batch noturno).
  Estágio 4  TIER (soft)  : OURO/PRATA c/ decisor · BRONZE sem decisor · PIPELINE se só-estimativa

Modo shadow (default): permitir_externo=False -> não chama web_search/BrasilAPI/Hunter,
apenas marca origem='externo_pendente'. Mantém o portão grátis e determinístico.
"""
import os
import re
import unicodedata
import yaml
import psycopg2
import psycopg2.extras

_CFG_PATH = os.path.join(os.path.dirname(__file__), "obra_classificacao.yaml")
_CFG = None
TIER_RANK = {"PIPELINE": 0, "BRONZE": 1, "PRATA": 2, "OURO": 3}


def cfg():
    global _CFG
    if _CFG is None:
        with open(_CFG_PATH, encoding="utf-8") as fh:
            _CFG = yaml.safe_load(fh)
    return _CFG


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "wins_hub"),
        user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""),
    )


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def valida_cnpj(cnpj):
    """DV padrão Receita. Espera 14 dígitos."""
    if not cnpj or not re.fullmatch(r"\d{14}", cnpj):
        return False
    if cnpj == cnpj[0] * 14:
        return False
    def dv(base, pesos):
        s = sum(int(d) * p for d, p in zip(base, pesos))
        r = s % 11
        return "0" if r < 2 else str(11 - r)
    d1 = dv(cnpj[:12], [5,4,3,2,9,8,7,6,5,4,3,2])
    d2 = dv(cnpj[:12] + d1, [6,5,4,3,2,9,8,7,6,5,4,3,2])
    return cnpj[12:] == d1 + d2


# ---------------- setor / dedup (read-only) ----------------
_SETORES = None
def setores_validos(conn):
    global _SETORES
    if _SETORES is None:
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT setor FROM setor_categorias")
            _SETORES = {r[0] for r in c.fetchall()}
    return _SETORES


def eh_guarda_chuva(conn, cnpj):
    """Heurística: CNPJ marcado suspeito/guarda-chuva na base de obras."""
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM obras WHERE cnpj=%s AND cnpj_status IN ('suspeito') LIMIT 1", (cnpj,))
        return c.fetchone() is not None


def acha_duplicata(conn, nome, empresa, uf, exclude_id=None):
    if not empresa or not uf:
        return None
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id FROM obras
                WHERE uf=%s AND lower(empresa)=lower(%s)
                  AND similarity(lower(nome), lower(%s)) > 0.6
                  AND (%s::uuid IS NULL OR id <> %s::uuid)
                LIMIT 1""", (uf, empresa, nome or "", exclude_id, exclude_id))
            r = c.fetchone()
            return str(r[0]) if r else None
    except Exception:
        return None


# ---------------- FASE 0 — enriquecimento interno (custo zero) ----------------
def fase0_interno(conn, cnpj):
    out = {"razao": None, "municipio": None, "email_forn": None,
           "dominio": None, "decisor": None,
           "origem": {"cnpj": None, "dominio": None, "decisor": None}}
    if not cnpj:
        return out
    raiz = cnpj[:8]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        # 1) fornecedores (full -> raiz, usa idx_fornecedores_cnpj_raiz)
        c.execute("SELECT razao_social, nome_fantasia, municipio_nome, email FROM fornecedores WHERE cnpj=%s LIMIT 1", (cnpj,))
        f = c.fetchone()
        origem_cnpj = "interno_full" if f else None
        if not f:
            c.execute("SELECT razao_social, nome_fantasia, municipio_nome, email FROM fornecedores WHERE substring(cnpj,1,8)=%s LIMIT 1", (raiz,))
            f = c.fetchone()
            origem_cnpj = "interno_raiz" if f else None
        if f:
            out["razao"] = f["razao_social"] or f["nome_fantasia"]
            out["municipio"] = f["municipio_nome"]
            out["email_forn"] = f["email"]
            out["origem"]["cnpj"] = origem_cnpj
        # 2) empresa_dominios (full/raiz/holding)
        c.execute("""SELECT dominio, holding_dominio FROM empresa_dominios
                     WHERE (cnpj=%s OR substring(cnpj,1,8)=%s)
                     ORDER BY (dominio IS NOT NULL) DESC LIMIT 1""", (cnpj, raiz))
        d = c.fetchone()
        if d and (d["dominio"] or d["holding_dominio"]):
            out["dominio"] = d["dominio"] or d["holding_dominio"]
            out["origem"]["dominio"] = "interno"
        # 3) decisores_preservados (reuso CRM)
        c.execute("""SELECT nome, cargo, email FROM decisores_preservados
                     WHERE (cnpj=%s OR substring(cnpj,1,8)=%s) AND COALESCE(email,'')<>''
                     LIMIT 1""", (cnpj, raiz))
        dec = c.fetchone()
        if dec:
            out["decisor"] = dict(dec)
            out["origem"]["decisor"] = "interno"
    return out


# ---------------- avaliação principal ----------------
def shadow_hook(obras, fonte_meta, conn, log=None):
    """SHADOW: avalia obras (lista de dicts) e loga resumo, SEM inserir nada.
    Ativa só com env PORTAO_SHADOW=1. Totalmente isolado — nunca levanta p/ o captador."""
    try:
        if os.getenv("PORTAO_SHADOW") != "1":
            return
        p = d = 0
        motivos = {}
        for o in obras:
            try:
                v = avaliar(o, fonte_meta, conn)
            except Exception:
                continue
            if v["passou"]:
                p += 1
            else:
                d += 1
                motivos[v["motivo"]] = motivos.get(v["motivo"], 0) + 1
        msg = f"[PORTAO-SHADOW] {fonte_meta.get('fonte')}: PASSA={p} DESCARTA={d} motivos={motivos}"
        (log.info if log else print)(msg)
    except Exception as e:
        if log:
            log.warning(f"[PORTAO-SHADOW] desativado (erro): {e!r}")


def avaliar(obra, fonte_meta, conn, permitir_externo=False):
    c = cfg()
    fonte = (fonte_meta or {}).get("fonte", "")
    fonte_tipo = (fonte_meta or {}).get("fonte_tipo", "OFICIAL")
    nome = obra.get("nome", "") or ""
    desc = obra.get("descricao", "") or ""
    setor = obra.get("setor")
    valor = obra.get("valor_estimado")
    cnpj = (obra.get("cnpj") or "").strip() or None
    empresa = obra.get("empresa")
    uf = obra.get("uf")
    capex_fonte = obra.get("capex_fonte")

    def reject(motivo, estagio):
        return {"passou": False, "motivo": motivo, "estagio": estagio, "tier": None,
                "origem_resolucao": {}, "hunter": {"inline": False, "enfileirado": False}}

    # --- Estágio 2: PORTÃO-DURO ---
    if any(d in fonte for d in c["politicas"]["dumpers_aposentar"]):
        return reject("fonte_aposentada", 2)

    if fonte_tipo == "NOTICIA":
        h = obra.get("haiku_extraiu") or {}
        faltam = [k for k in c["politicas"]["noticia_campos_obrigatorios"]
                  if not h.get(k) and not obra.get(k if k != "valor" else "valor_estimado")]
        if faltam:
            return reject("noticia_campos_incompletos", 2)
        # popula a partir do Haiku
        cnpj = cnpj or h.get("cnpj")
        setor = setor or h.get("setor")
        valor = valor or h.get("valor")
        empresa = empresa or h.get("empresa")

    texto = _norm(f"{nome} {desc}")
    if any(_norm(k) in texto for k in c["exclusao_hard"]):
        return reject("nao_e_obra_exclusao_hard", 2)

    if not setor or setor == "OUTRO" or setor not in setores_validos(conn):
        return reject("setor_fora_categorias", 2)

    if not cnpj:
        return reject("cnpj_ausente", 2)  # resolução por nome = externo (Fase 2)
    if not valida_cnpj(cnpj):
        return reject("cnpj_invalido", 2)
    if eh_guarda_chuva(conn, cnpj):
        return reject("cnpj_guarda_chuva", 2)

    dup = acha_duplicata(conn, nome, empresa, uf, obra.get("_self_id"))
    if dup:
        return {"passou": False, "motivo": "duplicata", "estagio": 2, "dup_id": dup,
                "tier": None, "origem_resolucao": {}, "hunter": {"inline": False, "enfileirado": False}}

    # --- Estágio 3: ENRIQUECE (Fase 0 interna; externo só marcado em shadow) ---
    interno = fase0_interno(conn, cnpj)
    tem_decisor = interno["decisor"] is not None
    valor_ok = (valor is not None and valor >= 10_000_000)

    # critério de valor: >=10mi OU já tem decisor reaproveitável
    if not valor_ok and not tem_decisor:
        return reject("abaixo_criterio_valor_sem_decisor", 2)

    origem = {
        "cnpj": interno["origem"]["cnpj"] or ("externo_pendente" if permitir_externo is False else "externo"),
        "dominio": interno["origem"]["dominio"] or "externo_pendente",
        "decisor": interno["origem"]["decisor"] or "externo_pendente",
    }
    email_decisor = (interno["decisor"] or {}).get("email") if tem_decisor else None
    hunter = {"inline": False, "enfileirado": email_decisor is None}  # Hunter só batch noturno

    # --- Estágio 4: TIER (soft) ---
    estimativa = capex_fonte in (c["soft_nao_rejeita"]["capex_estimativa"])
    if tem_decisor and email_decisor:
        tier = "PRATA"            # reusado: re-scoring de confiança decide OURO depois
    elif estimativa and not tem_decisor:
        tier = "PIPELINE"
    elif valor_ok:
        tier = "BRONZE"
    else:
        tier = "PIPELINE"

    return {
        "passou": True, "motivo": None, "estagio": 4, "tier": tier,
        "origem_resolucao": origem,
        "enriquecimento": {
            "razao": interno["razao"], "municipio_interno": interno["municipio"],
            "dominio": interno["dominio"], "decisor": interno["decisor"],
            "municipio_ausente_soft": not (uf and obra.get("municipio")),
            "capex_estimativa_soft": estimativa,
        },
        "hunter": hunter,
    }
