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
import json
import urllib.request
import unicodedata
import yaml
import psycopg2
import psycopg2.extras

_CFG_PATH = os.path.join(os.path.dirname(__file__), "obra_classificacao.yaml")
# Kill-switch operacional: `touch` deste arquivo desliga o enforce SEM restart de container.
_DISABLE_FLAG = os.path.join(os.path.dirname(__file__), "PORTAO_DISABLED")
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


def _norm_nome(s):
    return re.sub(r"\s+", " ", _norm(s or "")).strip()


def acha_duplicata(conn, nome, cnpj, exclude_id=None, exclude_id_externo=None):
    """Dedup por CHAVE COMPOSTA: CNPJ_raiz + nome_normalizado.
    Preserva projetos distintos do mesmo complexo/empresa (UFV-A vs UFV-B têm nomes
    normalizados diferentes) e só colapsa re-imports idênticos. Substitui o fuzzy
    similarity>0.6 que colapsava irmãs do ANEEL.

    CROSS-SOURCE: `exclude_id_externo` é o id_externo da obra que está ENTRANDO.
    Linhas com o MESMO id_externo são re-pulls da própria fonte (devem dar UPDATE via
    ON CONFLICT, não rejeição) → ignoradas aqui. Match sobrevivente = obra de fonte/id
    diferente porém mesma operação (ex.: bndes_financiamento vs bndes_saneamento) → dup."""
    if not cnpj or not re.fullmatch(r"\d{14}", cnpj) or not nome:
        return None
    nn = _norm_nome(nome)
    if not nn:
        return None
    raiz = cnpj[:8]
    try:
        with conn.cursor() as c:
            # só canônicos VISÍVEIS contam como alvo de dup: uma irmã já ocultada (ex.: pela
            # purga de dedup) não deve barrar o canônico nem reaparecer como match.
            c.execute("SELECT id, nome, id_externo FROM obras WHERE substring(cnpj,1,8)=%s AND visivel", (raiz,))
            for rid, rn, rext in c.fetchall():
                if exclude_id and str(rid) == str(exclude_id):
                    continue
                # re-pull da mesma fonte (mesmo id_externo) → não é dup; ON CONFLICT atualiza
                if exclude_id_externo and rext and str(rext) == str(exclude_id_externo):
                    continue
                if _norm_nome(rn) == nn:
                    return str(rid)
        return None
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


# ---------------- enriquecimento externo (free-first; Hunter NUNCA aqui) ----------------
def _brasilapi_qsa(cnpj):
    """GET brasilapi.com.br/api/cnpj — razão social + QSA. Free. None em erro."""
    if not cnpj or not re.fullmatch(r"\d{14}", cnpj):
        return None
    try:
        req = urllib.request.Request(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}",
            headers={"User-Agent": "wins-portao"})
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception:
        return None


def enriquecer_inline(obra, interno, conn, permitir_externo=False, web_search_fn=None):
    """Ordem: INTERNO (já em `interno`) -> web_search free-first -> BrasilAPI QSA.
    Hunter NUNCA aqui (só enfileira no batch noturno). Em permitir_externo=False (shadow)
    apenas marca o que iria a externo, sem chamar nada."""
    res = {"razao": interno["razao"], "dominio": interno["dominio"],
           "decisor": interno["decisor"], "origem": dict(interno["origem"])}
    falta_dom = not res["dominio"]
    falta_dec = res["decisor"] is None
    if not permitir_externo:
        if falta_dom:
            res["origem"]["dominio"] = "externo_pendente"
        if falta_dec:
            res["origem"]["decisor"] = "externo_pendente"
        if res["origem"].get("cnpj") is None:
            res["origem"]["cnpj"] = "externo_pendente"
        return res
    # LIVE — web_search free-first (domínio/decisor); função injetada pelo captador (Serper)
    if web_search_fn and (falta_dom or falta_dec):
        try:
            ws = web_search_fn(obra) or {}
            if falta_dom and ws.get("dominio"):
                res["dominio"] = ws["dominio"]; res["origem"]["dominio"] = "web_search"
            if falta_dec and ws.get("decisor"):
                res["decisor"] = ws["decisor"]; res["origem"]["decisor"] = "web_search"
        except Exception:
            pass
    # BrasilAPI QSA — razão/sócios se CNPJ não resolveu no interno
    if not res["razao"]:
        qsa = _brasilapi_qsa(obra.get("cnpj"))
        if qsa:
            res["razao"] = qsa.get("razao_social")
            res["origem"]["cnpj"] = res["origem"].get("cnpj") or "brasilapi"
    return res


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


def avaliar(obra, fonte_meta, conn, permitir_externo=False, web_search_fn=None, checar_dup=True):
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
    id_externo = obra.get("id_externo")

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

    # dedup do portão só p/ fontes SEM chave natural (notícias). Oficiais usam
    # ON CONFLICT (id_externo) -> checar_dup=False evita rejeitar re-pulls que devem dar UPDATE.
    if checar_dup:
        dup = acha_duplicata(conn, nome, cnpj, obra.get("_self_id"), exclude_id_externo=id_externo)
        if dup:
            return {"passou": False, "motivo": "duplicata", "estagio": 2, "dup_id": dup,
                    "tier": None, "origem_resolucao": {}, "hunter": {"inline": False, "enfileirado": False}}

    # --- Estágio 3: ENRIQUECE — INTERNO primeiro, depois externo free-first (Hunter nunca) ---
    interno = fase0_interno(conn, cnpj)
    valor_ok = (valor is not None and valor >= 10_000_000)

    # critério de valor: >=10mi OU já tem decisor interno reaproveitável (não gasta externo em obra pequena)
    if not valor_ok and interno["decisor"] is None:
        return reject("abaixo_criterio_valor_sem_decisor", 2)

    enr = enriquecer_inline(obra, interno, conn, permitir_externo, web_search_fn)
    decisor = enr["decisor"]
    email_decisor = (decisor or {}).get("email")
    hunter = {"inline": False, "enfileirado": email_decisor is None}  # Hunter só batch noturno

    # --- Estágio 4: TIER (soft) ---
    estimativa = capex_fonte in (c["soft_nao_rejeita"]["capex_estimativa"])
    if decisor and email_decisor:
        tier = "PRATA"            # reusado: re-scoring de confiança decide OURO depois
    elif estimativa and not decisor:
        tier = "PIPELINE"
    elif valor_ok:
        tier = "BRONZE"
    else:
        tier = "PIPELINE"

    return {
        "passou": True, "motivo": None, "estagio": 4, "tier": tier,
        "origem_resolucao": enr["origem"],
        "enriquecimento": {
            "razao": enr["razao"], "municipio_interno": interno["municipio"],
            "dominio": enr["dominio"], "decisor": decisor,
            "municipio_ausente_soft": not (uf and obra.get("municipio")),
            "capex_estimativa_soft": estimativa,
        },
        "hunter": hunter,
    }


# ---------------- ENFORCE de lote p/ captadores oficiais ----------------
# Posições padrão das tuplas de INSERT dos captadores oficiais (aneel/bndes/antt/ibama/cvm/deb):
# layout confirmado idêntico nos enforced: id_externo=0, nome=1 ... valor_estimado=7.
DEFAULT_IDX = {"id_externo": 0, "nome": 1, "empresa": 2, "cnpj": 3, "setor": 4,
               "municipio": 5, "uf": 6, "valor_estimado": 7, "capex_fonte": None}

# Fontes onde a MESMA operação reaparece sob fonte/id_externo diferente (dup cross-source)
# OU sob id_externo distinto na própria fonte (dup interno). Pra elas ligamos o dedup por
# CNPJ_raiz+nome_norm (checar_dup). NÃO inclui aneel/ibama: têm agregação própria por
# complexo e podem ter irmãs de nome idêntico (ex.: Kuara, 120 UFVs) que NÃO são dup.
DEDUP_CROSS_FONTES = {
    "bndes_financiamento", "bndes_saneamento", "bndes_saude",
    "debentures_infra", "cvm_ipe",
}


def _persistir_dominio(conn, cnpj, empresa, dominio):
    """Cacheia domínio resolvido por Serper em empresa_dominios -> próximo ciclo resolve
    interno (custo Serper vira pontual, não recorrente). Best-effort."""
    if not cnpj or not empresa or not dominio:
        return
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO empresa_dominios (cnpj, empresa_nome, dominio, dominio_status, confianca)
                VALUES (%s, %s, %s, 'inferido_portao', 2)
                ON CONFLICT (cnpj) DO UPDATE
                  SET dominio = EXCLUDED.dominio
                  WHERE empresa_dominios.dominio IS NULL OR empresa_dominios.dominio = ''
            """, (cnpj, empresa[:255], dominio))
        conn.commit()
    except Exception:
        conn.rollback()


def filtrar_e_enriquecer(obras, fonte, conn, idx=None, web_search_fn=None,
                         externo_cap=None, checar_dup=False, log=None):
    """ENFORCE p/ captador oficial: recebe a lista de tuplas que iria pro execute_values,
    DESCARTA as que não passam o portão e devolve a lista filtrada. Enriquece domínio via
    Serper (free-first) até `externo_cap` por ciclo, cacheando em empresa_dominios.
    À prova de falha: kill-switch env PORTAO_BYPASS=1 ou qualquer erro -> devolve a lista
    original intacta (fail-open, nunca perde obra por bug do portão)."""
    idx = idx or DEFAULT_IDX
    if externo_cap is None:
        externo_cap = int(os.getenv("PORTAO_SERPER_CAP", "25"))  # cap Serper/ciclo (timeout-safe)
    _log = (log.info if log else print)
    # dedup cross-source: liga p/ fontes dup-prone (BNDES família/debêntures/cvm) mesmo que o
    # caller não peça. Re-pulls da própria fonte continuam indo p/ ON CONFLICT (acha_duplicata
    # ignora mesmo id_externo). Demais fontes seguem só ON CONFLICT (checar_dup=False).
    checar_dup_eff = checar_dup or (fonte in DEDUP_CROSS_FONTES)
    # kill-switch: env OU arquivo-flag (touch PORTAO_DISABLED -> off instantâneo, sem restart)
    if os.getenv("PORTAO_BYPASS") == "1" or os.path.exists(_DISABLE_FLAG):
        _log(f"[PORTAO] DESLIGADO — {fonte}: passthrough ({len(obras)} obras)")
        return obras
    try:
        manter, motivos, serper = [], {}, 0
        for o in obras:
            try:
                obra = {k: (o[i] if (i is not None and i < len(o)) else None)
                        for k, i in idx.items()}
                v = avaliar(obra, {"fonte": fonte, "fonte_tipo": "OFICIAL"}, conn,
                            permitir_externo=False, checar_dup=checar_dup_eff)
            except Exception:
                manter.append(o)  # fail-open por linha
                continue
            if not v["passou"]:
                motivos[v["motivo"]] = motivos.get(v["motivo"], 0) + 1
                continue
            manter.append(o)
            # enriquecimento externo de domínio: OPT-IN explícito (PORTAO_ENRICH_SERPER=1).
            # OFF por padrão -> ingestão NÃO depende de rede/Serper; domínio fica p/ o pipeline
            # assíncrono (populate_dominios). Soft/capado/cacheado; nunca afeta passar/reprovar.
            if (web_search_fn and os.getenv("PORTAO_ENRICH_SERPER") == "1"
                    and serper < externo_cap
                    and v["origem_resolucao"].get("dominio") == "externo_pendente"):
                try:
                    dom = (web_search_fn(obra) or {}).get("dominio")
                    if dom:
                        _persistir_dominio(conn, obra.get("cnpj"), obra.get("empresa"), dom)
                        serper += 1
                except Exception:
                    pass
        # guardrail anti-idx-bug: derrubar >65% por motivo ESTRUTURAL (setor/cnpj/não-obra)
        # sinaliza idx errado -> fail-open p/ NUNCA zerar um captador por bug de mapeamento.
        # Descarte de DUPLICATA é legítimo e pode ser alto (ex.: bndes_saneamento é quase todo
        # dup de bndes_financiamento) -> conta dups como "manteria" no cálculo do guardrail.
        dups = motivos.get("duplicata", 0)
        if len(obras) >= 10 and (len(manter) + dups) < 0.35 * len(obras):
            _log(f"[PORTAO] {fonte}: SUSPEITO {len(manter)}/{len(obras)} (<35% estrutural) — "
                 f"fail-open (idx provavelmente errado) motivos={motivos}")
            return obras
        _log(f"[PORTAO] {fonte}: mantidas {len(manter)}/{len(obras)} | "
             f"descartadas {len(obras)-len(manter)} (dups={dups}) {motivos} | serper={serper}")
        return manter
    except Exception as e:
        _log(f"[PORTAO] {fonte}: ERRO no filtro, fail-open ({e!r})")
        return obras
