"""
enrichment_auto_job.py — Pipeline canônico de 8 passos aplicado em batch.

Acionado por cron diário 09:00 UTC (06:00 BRT) após captadores noturnos:
  - 05:00 UTC orchestrator (captação 6 fontes)
  - 05:45 UTC validador_nivel1 (popula validacao_obra_at)
  - 06:00 UTC enriquecer_fila (Mari)
  → 09:00 UTC enrichment_auto_job (este script)

Skill: .claude/skills/nova-obra-enrichment/SKILL.md
Tag: v1.4.2-enrichment-auto-job

Uso:
  docker exec wins_hub-api-1 python /app/scripts/enrichment_auto_job.py --dry-run
  docker exec wins_hub-api-1 python /app/scripts/enrichment_auto_job.py --commit [--limit 10]
"""
import sys
sys.path.insert(0, "/app")

import argparse
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from services.matchmaking import DB_CONFIG
from sales_intelligence.decisor_gate import decisor_inserivel
from sales_intelligence.camada2_pattern_detection.classificar_pessoa import classificar_local_part

# ───────────────────────── Safeguards / constantes ─────────────────────────
MAX_OBRAS_PER_RUN = 200
HUNTER_MIN_SALDO = 50
HUNTER_MAX_CALLS_PER_OBRA = 4
SERPER_LINKEDIN_CALLS = 2
SERPER_MARI_CALLS = 4
SERPER_TELEFONE_CALLS = 1
CAPEX_MIN = 1_000_000  # pipeline_ev 01062026: baixado de 50M -> 1M (cobre municipal)
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0 Safari/537.36"
TODAY_TAG = datetime.now().strftime("%Y%m%d")
MARKER = f"enrichment_auto:v1:{TODAY_TAG}"

# Tiers de prioridade
def _build_sql_obras_prioridade(admin_bulk: bool = False) -> str:
    """Monta SQL de prioridade. Em --admin-bulk, remove filtro criado_em (24h) pra varrer backlog."""
    time_filter = "" if admin_bulk else "o.criado_em >= now() - interval '72 hours' AND"
    return f"""
WITH cand AS (
  SELECT
    o.id, o.nome, o.empresa, o.cnpj, o.setor, o.uf,
    o.valor_estimado, o.fonte, o.fonte_tipo,
    o.classificacao_computed, o.criado_em,
    -- pipeline_ev 01062026: tiers sem corte (ELSE = 4 em vez de NULL)
    CASE
      WHEN o.valor_estimado >= 1e9 THEN 1
      WHEN o.valor_estimado >= 500e6 THEN 2
      WHEN o.valor_estimado >= 100e6 THEN 3
      ELSE 4
    END AS prioridade
  FROM obras o
  WHERE {time_filter}
        o.valor_estimado >= %s
    -- pipeline_ev 01062026: cnpj opcional (DOU/PNCP municipal sem CNPJ)
    AND o.nivel1_nome IS NULL
    AND o.motivo_invisivel IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM decisores_obra d
      WHERE d.obra_id = o.id AND d.excluido_em IS NULL
    )
    AND NOT EXISTS (
      SELECT 1 FROM decisores_obra d
      WHERE d.obra_id = o.id
        AND d.registrado_por LIKE %s
    )
)
SELECT *
FROM cand
-- pipeline_ev 01062026: tier 4 passa (sem corte hardcoded)
ORDER BY prioridade, valor_estimado DESC NULLS LAST
LIMIT %s;
"""


SQL_OBRAS_PRIORIDADE = _build_sql_obras_prioridade(admin_bulk=False)

# ───────────────────────── Hunter helpers ──────────────────────────────────
def hunter_saldo(api_key: str) -> int:
    req = urllib.request.Request(
        "https://api.hunter.io/v2/account",
        headers={"User-Agent": UA, "Authorization": f"Bearer {api_key}"},
    )
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    r = d.get("data", {}).get("requests", {}).get("searches", {})
    return int(r.get("available", 0)) - int(r.get("used", 0))


def hunter_email_finder(api_key: str, domain: str, first: str, last: str) -> dict:
    qs = urllib.parse.urlencode(
        {"domain": domain, "first_name": first, "last_name": last}
    )
    req = urllib.request.Request(
        f"https://api.hunter.io/v2/email-finder?{qs}",
        headers={"User-Agent": UA, "Authorization": f"Bearer {api_key}"},
    )
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return r.get("data", {}) or {}
    except urllib.error.HTTPError as e:
        log.warning(f"hunter email-finder HTTP {e.code}: {e.read()[:200].decode()}")
        return {}


# ───────────────────────── Serper helpers ──────────────────────────────────
def serper_search(api_key: str, query: str, num: int = 10) -> list[dict]:
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=json.dumps({"q": query, "num": num, "gl": "br", "hl": "pt-br"}).encode(),
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return d.get("organic", []) or []
    except Exception as e:
        log.warning(f"serper exception q={query[:60]!r}: {e}")
        return []


# ───────────────────────── Domain validation (Passo 2) ─────────────────────
def get_dominio_validado(cur, cnpj: str | None) -> tuple[str | None, str]:
    """Retorna (dominio, motivo). Se confianca < 5 ou metodo suspeito, retorna None."""
    if not cnpj:
        return None, "sem_cnpj"
    cur.execute(
        """SELECT dominio, confianca, validacao_metodo
           FROM empresa_dominios WHERE cnpj=%s""",
        (cnpj,),
    )
    row = cur.fetchone()
    if not row or not row.get("dominio"):
        return None, "nao_cacheado"
    metodo = (row.get("validacao_metodo") or "").lower()
    if any(s in metodo for s in ("agressivo", "descoberta_automatica", "domain_search")):
        return None, f"metodo_suspeito:{metodo}"
    if (row.get("confianca") or 0) < 4:
        return None, f"confianca_baixa:{row.get('confianca')}"
    return row["dominio"], "ok"


# ───────────────────────── LinkedIn search (Passo 3) ───────────────────────
def linkedin_search(serper_key: str, empresa: str) -> list[dict]:
    queries = [
        f'site:linkedin.com/in "{empresa}" "diretor" OR "gerente" '
        f'("capex" OR "investimentos" OR "projetos" OR "implantação")',
        f'site:linkedin.com/in "{empresa}" "diretor" OR "gerente" '
        f'("suprimentos" OR "compras" OR "procurement" OR "engenharia")',
    ]
    hits = []
    for q in queries:
        for r in serper_search(serper_key, q, num=10):
            snippet = (r.get("snippet") or "").lower()
            if "ex-" in snippet or "former" in snippet:
                continue
            hits.append(r)
    return hits


# ───────────────────────── Candidate parsing ───────────────────────────────
NAME_RE = re.compile(r"^([A-Z][a-záàâãéêíóôõúç]+(?:\s+[A-Z][a-záàâãéêíóôõúç]+){1,3})")


def parse_candidato(hit: dict, empresa: str) -> dict | None:
    title = hit.get("title") or ""
    link = hit.get("link") or ""
    snippet = hit.get("snippet") or ""
    # title format: "Nome Sobrenome - Cargo - Empresa | LinkedIn"
    parts = [p.strip() for p in title.split(" - ")]
    if len(parts) < 2:
        return None
    nome_raw = parts[0]
    m = NAME_RE.match(nome_raw)
    if not m:
        return None
    nome = m.group(1)
    cargo = parts[1]

    # v1.4.5 Patch 2a: cap cargo length (cargos reais raramente > 80 chars)
    if len(cargo) > 80:
        return None

    # v1.4.5 Patch 2b: rejeita cargo contaminado com razão social (parse error)
    # Detectado no incidente Trident: cargo virou "TRIDENT ENERGY DO BRASIL LTDA (P..."
    if empresa:
        empresa_tokens = [t for t in re.sub(r'[^a-z0-9\s]', ' ', empresa.lower()).split()
                          if len(t) >= 4][:3]
        if len(empresa_tokens) >= 2:
            cargo_norm = re.sub(r'[^a-z0-9\s]', ' ', cargo.lower())
            phrase = ' '.join(empresa_tokens[:2])
            if phrase in cargo_norm:
                return None

    # exige empresa correta no title/snippet
    if empresa.lower().split()[0] not in (title + " " + snippet).lower():
        return None
    return {
        "nome": nome,
        "cargo": cargo,
        "linkedin_url": link,
        "raw_title": title,
        "raw_snippet": snippet[:300],
    }


def rank_candidatos(cands: list[dict]) -> list[dict]:
    """Prioriza Diretor > Gerente > Coordenador; áreas capex/implantação > suprimentos > engenharia."""
    def score(c):
        cargo = c["cargo"].lower()
        s = 0
        if "diretor" in cargo or "director" in cargo:
            s += 30
        elif "gerente" in cargo or "manager" in cargo:
            s += 20
        elif "coordenador" in cargo:
            s += 10
        for area in ("implantação", "capex", "investimento", "transmissão"):
            if area in cargo:
                s += 15
                break
        for area in ("suprimento", "procurement", "compras"):
            if area in cargo:
                s += 10
                break
        return -s  # menor = melhor pra sort
    # dedup por nome
    seen, out = set(), []
    for c in sorted(cands, key=score):
        if c["nome"] in seen:
            continue
        seen.add(c["nome"])
        out.append(c)
    return out


# ───────────────────────── Telefone search (Passo 6) ───────────────────────
PHONE_RE = re.compile(r"\+?\s?55?\s?\(?(?:0?\s?)?(\d{2})\)?[\s\-]?\d{4,5}[\s\-]?\d{4}")


def telefone_corporativo(serper_key: str, empresa: str) -> str | None:
    q = f'"{empresa}" "fale conosco" OR "contato" telefone'
    for r in serper_search(serper_key, q, num=5):
        text = (r.get("snippet") or "") + " " + (r.get("title") or "")
        m = PHONE_RE.search(text)
        if m:
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) >= 10:
                if not digits.startswith("55"):
                    digits = "55" + digits
                return "+" + digits
    return None


# ───────────────────────── Persistência (Passo 7) ──────────────────────────
def persistir_decisor(cur, obra: dict, cand: dict, email: str, score: int,
                       linkedin_url: str, telefone: str | None,
                       extra_componentes: dict | None = None) -> bool:
    nome = cand["nome"]
    cargo = cand["cargo"]
    cnpj_raiz = (obra.get("cnpj") or "")[:8] if obra.get("cnpj") else None
    allowed, motivo = decisor_inserivel(cur, nome, cargo, obra.get("empresa") or "", cnpj_raiz)
    if not allowed:
        log.info(f"  ✗ decisor_gate rejeita {nome!r}: {motivo}")
        return False
    componentes = {
        "fonte_pipeline": "enrichment_auto_job_v1",
        "linkedin_url_serper": linkedin_url,
        "hunter_score": score,
        "hunter_email": email,
        "obra_capex": float(obra["valor_estimado"]),
        "obra_fonte": obra["fonte"],
        "decisor_gate_motivo": motivo,
    }
    if extra_componentes:
        componentes.update(extra_componentes)
    # Tipo cargo
    cargo_lower = cargo.lower()
    if "gerente" in cargo_lower and ("projeto" in cargo_lower or "implantação" in cargo_lower):
        tipo = "GERENTE_PROJETOS"
    elif "gerente" in cargo_lower and "engenharia" in cargo_lower:
        tipo = "GERENTE_ENGENHARIA"
    elif "gerente" in cargo_lower and ("suprimento" in cargo_lower or "compra" in cargo_lower):
        tipo = "GERENTE_SUPRIMENTOS"
    elif "coordenador" in cargo_lower and "obra" in cargo_lower:
        tipo = "COORDENADOR_OBRAS"
    else:
        tipo = "OUTRO"
    if email:
        confianca = max(70, min(score, 97))
        fonte_decisor = 'serper_linkedin+hunter_email_finder'
    else:
        # Hunter esgotado: lead identificado via LinkedIn, EMAIL PENDENTE. Confiança
        # capada em 69 → recompute_classificacao_obra dá PRATA (>=50), NUNCA OURO
        # (OURO exige max_score>=70 — e há decisor c/ linkedin_url, então cap evita
        # promover lead sem email a OURO). Backfill de email quando quota Hunter voltar.
        confianca = max(50, min(int(score) if score else 55, 69))
        fonte_decisor = 'serper_linkedin (email_pendente)'
        componentes["email_pendente"] = True
    cur.execute(
        """INSERT INTO decisores_obra
           (obra_id, nome, cargo, linkedin_url, email, telefone,
            fonte, registrado_por, tipo_cargo, confianca_match,
            hipotese_replicacao, confianca_match_componentes,
            confianca_match_calculada_em)
           VALUES (%s,%s,%s,%s,%s,%s,
                   %s, %s, %s, %s,
                   NULL, %s, now())
           ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
           RETURNING id""",
        (obra["id"], nome, cargo, linkedin_url, email, telefone,
         fonte_decisor, MARKER, tipo, confianca, Json(componentes)),
    )
    row = cur.fetchone()
    if row:
        log.info(f"  ✓ INSERT decisor {nome} ({cargo}) email={email} score={score}")
        return True
    log.info(f"  ⊘ skip decisor {nome}: já existe (ON CONFLICT)")
    return False


def _persistir_lead_sem_email(cur, obra: dict, cand) -> bool:
    """Degradação graciosa (11/06): persiste candidato do LinkedIn como decisor
    SEM email quando o Hunter está esgotado. email_pendente → recompute dá PRATA
    (nunca OURO). Passa pelo decisor_gate via persistir_decisor (email=None)."""
    nome_partes = (getattr(cand, "nome_pessoa", "") or "").split()
    if len(nome_partes) < 2:
        return False
    cand_dict = {
        "nome": cand.nome_pessoa,
        "cargo": cand.cargo_raw or "",
        "linkedin_url": (f"https://br.linkedin.com/in/{cand.linkedin_slug}"
                          if getattr(cand, "linkedin_slug", None) else ""),
    }
    extra = {
        "fonte_pipeline": "cascade_admin_v1.4.8",
        "email_status": "email_pendente_hunter_esgotado",
        "cargo_raw_original": cand.cargo_raw,
        "tipo_cargo_mari": getattr(cand, "tipo_cargo", None),
    }
    return persistir_decisor(cur, obra, cand_dict, None, 0,
                             cand_dict["linkedin_url"], None, extra_componentes=extra)


_EVIDENCIA_STOP_TOKENS = {
    "ltda", "sociedade", "empresa", "grupo", "holding", "brasil", "participacoes",
    "energia", "energetica", "eletrica", "geracao", "engenharia", "construcao",
    "construtora", "industria", "comercio", "usina", "acucar", "alcool",
    "cooperativa", "central", "credito", "logistica", "transportes", "servicos",
    "agroindustrial", "incorporadora", "empreendimentos",
}


def _cand_tem_evidencia(cand, empresa: str) -> bool:
    """Gate anti-FP nome-so (12/06): exige vinculo minimo com a empresa.
    FPs Irani/Miridan/Laguna entraram como nome-so (titulo LK sem
    ' - Cargo - Empresa'): sem cargo/emp extraidos, bate_empresa e
    proximity_check da camada 3 sao pulados e o candidato chega ao persist
    sem NENHUMA validacao. Evidencia aceita (qualquer uma):
      a) confianca alta/media (bate_empresa ativou na camada 3)
      b) cargo_raw extraido do titulo (passou no proximity_check)
      c) token distintivo da empresa (>=4 chars, fora stoplist) no snippet
    """
    if getattr(cand, "confianca", "") in ("alta", "media"):
        return True
    if (getattr(cand, "cargo_raw", "") or "").strip():
        return True
    from unidecode import unidecode
    snippet = unidecode((getattr(cand, "snippet_origem", "") or "")).lower()
    emp = unidecode(empresa or "").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", emp)
              if len(t) >= 4 and t not in _EVIDENCIA_STOP_TOKENS]
    return any(t in snippet for t in tokens) if tokens and snippet else False


def cascade_backfill_emails_pendentes(cur, conn, obra: dict, dominio: str,
                                      hunter_key: str, budget) -> int:
    """Backfill (11/06): com Hunter disponível, busca o email dos decisores que
    ficaram email_pendente (persistidos quando o Hunter estava esgotado) e faz
    UPDATE — em vez de o ON CONFLICT DO NOTHING pular. Decisor ganha email +
    confianca>=70 → recompute promove a obra de PRATA para OURO."""
    obra_id = str(obra["id"])
    cur.execute(
        """SELECT id, nome FROM decisores_obra
           WHERE obra_id=%s::uuid AND excluido_em IS NULL
             AND (email IS NULL OR email='')
             AND confianca_match_componentes->>'email_status' = 'email_pendente_hunter_esgotado'""",
        (obra_id,),
    )
    pendentes = cur.fetchall()
    if not pendentes:
        return 0
    backfilled = 0
    for d in pendentes:
        if not budget.pode_hunter():
            break
        partes = (d["nome"] or "").split()
        if len(partes) < 2:
            continue
        first, last = partes[0], " ".join(partes[1:])
        r = hunter_email_finder(hunter_key, dominio, first, last)
        budget.hunter_calls += 1
        email = (r or {}).get("email")
        score = int((r or {}).get("score") or 0)
        v_status = ((r or {}).get("verification") or {}).get("status")
        if not email or score < 70 or v_status not in ("valid", "accept_all", None):
            log.info(f"  ✗ backfill {d['nome']}: email={email} score={score} status={v_status}")
            continue
        confianca = max(70, min(score, 97))
        cur.execute(
            """UPDATE decisores_obra SET
                   email=%s,
                   confianca_match=%s,
                   fonte='serper_linkedin+hunter_email_finder',
                   confianca_match_calculada_em=now(),
                   confianca_match_componentes = COALESCE(confianca_match_componentes,'{}'::jsonb)
                       || jsonb_build_object(
                            'email_status','hunter_verified_backfill',
                            'hunter_email', %s::text,
                            'hunter_score', %s::int,
                            'email_pendente', false,
                            'backfill_em', %s::text)
               WHERE id=%s AND excluido_em IS NULL""",
            (email, confianca, email, score, TODAY_TAG, d["id"]),
        )
        backfilled += 1
        log.info(f"  ✓ BACKFILL email {d['nome']}: {email} score={score} (PRATA→OURO se ≥70)")
    if backfilled:
        conn.commit()
    return backfilled


# ───────────────────────── Status enrichment tracking ────────────────────
def _classify_status(decisores_inseridos: int, skip_motivo: str | None) -> str:
    """Mapeia resultado do pipeline → status code curto pra obras.ultimo_enrichment_status."""
    if decisores_inseridos and decisores_inseridos > 0:
        return "success"
    sm = (skip_motivo or "").lower()
    if not sm:
        return "tried_zero"
    if "saldo" in sm or "hunter_saldo" in sm:
        return "skip_hunter_saldo"
    if "dominio" in sm:
        return "skip_sem_dominio"
    if "cargo" in sm or "razao_social" in sm:
        return "skip_cargo_invalido"
    if "candidat" in sm or "linkedin" in sm:
        return "skip_sem_candidatos"
    if "obra_nao_encontrada" in sm:
        return "skip_obra_inexistente"
    return "tried_zero"


def _update_obra_status(cur, conn, obra_id: str, status_code: str, skip_motivo: str | None):
    """UPDATE obras.ultimo_enrichment_* — chamado pelo final de processar_obra."""
    try:
        cur.execute(
            """UPDATE obras
               SET ultimo_enrichment_status = %s,
                   ultimo_enrichment_at = now(),
                   ultimo_enrichment_skip_motivo = %s
               WHERE id = %s::uuid""",
            (status_code, skip_motivo, obra_id),
        )
        conn.commit()
    except Exception as e:
        log.warning(f"_update_obra_status falhou para {obra_id}: {e}")
        conn.rollback()


# ───────────────────────── Single-obra mode (admin button) ────────────────
SQL_OBRA_ID_SINGLE = """
SELECT
  o.id, o.nome, o.empresa, o.cnpj, o.setor, o.uf,
  o.valor_estimado, o.fonte, o.fonte_tipo,
  o.classificacao_computed, o.criado_em,
  99 AS prioridade
FROM obras o WHERE o.id = %s::uuid
"""


_DOMAIN_BLOCKLIST_FRAGS = (
    'linkedin', 'facebook', 'instagram', 'twitter', 'wikipedia',
    'youtube', 'gov.br', 'rocketreach', 'signalhire', 'crunchbase',
    'leis.org', 'jusbrasil', 'consultacnpj', 'econodata', 'cnpjbiz',
)
_EMPRESA_SUFIXOS_GENERICOS = {
    'sa','s/a','s.a','s.a.','ltda','ltd','eireli','grupo','group','holding',
    'companhia','co','brasil','brazil','br','do','da','de','dos','das','e',
    'industria','indústria','industrial','agro','com','corp','corporation','inc',
    'saneamento','energia','energias','energetica','energética','logistica',
    'logística','transportes','transp','aeroportos','aeroporto','spe','empreendimentos',
    'geracao','geração','renovaveis','renováveis','participacoes','participações',
}


def _empresa_tokens_distintivos(empresa: str, min_len: int = 4) -> list[str]:
    """Mesma lógica do decisor_gate._tokens_distintivos — fontes únicas de verdade."""
    if not empresa:
        return []
    norm = re.sub(r'[^a-z0-9\s]', ' ', empresa.lower())
    return [t for t in norm.split()
            if t not in _EMPRESA_SUFIXOS_GENERICOS and len(t) >= min_len]


def discover_domain_via_serper(serper_key: str, empresa: str,
                                 hunter_key: str | None = None) -> str | None:
    """Descobre domínio canônico via Serper (modo admin --obra-id apenas).

    Defesa contra falsos positivos (incidente Trident/leis.org 24/05):
      (1) Blocklist de domínios social/diretórios/bases jurídicas
      (2) REJEITA se nenhum token distintivo da empresa aparece no domínio
      (3) Confirma via Hunter /domain-search se >= 1 email indexed (custo 1 call)

    Falha-fechada: retorna None se qualquer guard falhar — melhor 0 decisores
    que decisor de outra empresa.
    """
    if not empresa:
        return None

    tokens = _empresa_tokens_distintivos(empresa)
    if not tokens:
        log.warning(f"discover_domain: sem tokens distintivos em {empresa!r}, skip")
        return None

    hits = serper_search(serper_key, f'"{empresa}" site oficial OR "fale conosco"', num=5)
    counts: dict[str, int] = {}
    for h in hits:
        url = h.get('link') or ''
        m = re.search(r'https?://(?:www\.)?([^/]+)', url)
        if not m:
            continue
        d = m.group(1).lower()
        if any(s in d for s in _DOMAIN_BLOCKLIST_FRAGS):
            continue
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None

    # Testa candidatos em ordem de frequência
    for candidate, freq in sorted(counts.items(), key=lambda x: -x[1]):
        # Guard 2: token empresa↔domínio
        matched_tokens = [t for t in tokens if t in candidate]
        if not matched_tokens:
            log.warning(f"discover_domain: {candidate} REJEITADO (nenhum token de {tokens})")
            continue
        # Guard 3: Hunter domain-search confirmation
        if hunter_key:
            try:
                qs = urllib.parse.urlencode({'domain': candidate, 'limit': 1})
                req = urllib.request.Request(
                    f'https://api.hunter.io/v2/domain-search?{qs}',
                    headers={'User-Agent': UA, 'Authorization': f'Bearer {hunter_key}'}
                )
                r = json.loads(urllib.request.urlopen(req, timeout=20).read())
                emails_count = int((r.get('meta') or {}).get('results') or 0)
                if emails_count < 1:
                    log.warning(f"discover_domain: {candidate} REJEITADO (Hunter results={emails_count})")
                    continue
                log.info(f"discover_domain: {candidate} ACEITO (tokens={matched_tokens}, Hunter={emails_count} emails)")
                return candidate
            except Exception as e:
                log.warning(f"discover_domain: Hunter falhou em {candidate} ({e}), skip")
                continue
        else:
            log.info(f"discover_domain: {candidate} aceito sem Hunter check (tokens={matched_tokens})")
            return candidate
    return None


# ───────────────────────── Holding fallback v1.4.7 ─────────────────────────
# SPVs/concessionárias (ANTT/ANEEL/ANTAQ) frequentemente têm domínio próprio
# sem cobertura Hunter (ex.: ecoriominas.com.br retorna 0 pessoas, mas
# ecorodovias.com.br do grupo controlador retorna 5+ diretores). Fluxo:
#   1. Cache hit em empresa_dominios.holding_dominio (0 API calls)
#   2. BrasilAPI QSA → primeiro sócio PJ (CNPJ 14 dígitos sem mascara)
#   3. discover_domain_via_serper(nome_holding) — reusa o validador 3-guards
#   4. Persiste em empresa_dominios.holding_* pra próxima vez

def _brasilapi_qsa(cnpj: str) -> dict | None:
    """BrasilAPI v1/cnpj/{cnpj}. Free, sem chave. Retorna JSON ou None."""
    cnpj_clean = re.sub(r'\D', '', cnpj or '')
    if len(cnpj_clean) != 14:
        return None
    try:
        from urllib.request import Request, urlopen
        req = Request(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_clean}",
            headers={"User-Agent": "WiNS-Hub/1.4.7 enrichment"},
        )
        return json.loads(urlopen(req, timeout=10).read())
    except Exception as e:
        log.warning(f"  brasilapi_qsa({cnpj_clean}) falhou: {e}")
        return None


def _socio_pj_controlador(qsa_data: dict) -> tuple[str, str] | None:
    """Procura sócio PJ no QSA. BrasilAPI mascara CPFs com asterisks; CNPJs
    aparecem unmasked (14 dígitos). Retorna (cnpj_pj, nome_pj) ou None."""
    if not qsa_data:
        return None
    for s in (qsa_data.get("qsa") or []):
        raw_id = (s.get("cnpj_cpf_do_socio") or "").strip()
        digits = re.sub(r'\D', '', raw_id)
        if "*" not in raw_id and len(digits) == 14:
            return digits, (s.get("nome_socio") or "").strip()
    return None


def get_holding_dominio(cur, conn, cnpj: str, hunter_key: str, serper_key: str
                         ) -> tuple[str | None, str]:
    """Resolve domínio do controlador. Cache → BrasilAPI QSA → discover.
    Persiste em empresa_dominios.holding_* pra reuso. Retorna (dominio, motivo)."""
    if not cnpj:
        return None, "sem_cnpj"
    # 1. Cache hit (lookup direto, 0 API calls)
    cur.execute(
        """SELECT holding_dominio, holding_cnpj, holding_nome
           FROM empresa_dominios WHERE cnpj=%s""",
        (cnpj,),
    )
    row = cur.fetchone()
    if row and row.get("holding_dominio"):
        return row["holding_dominio"], f"cache_hit:{row.get('holding_nome') or '?'}"
    # 2. BrasilAPI QSA
    qsa_data = _brasilapi_qsa(cnpj)
    if not qsa_data:
        return None, "brasilapi_falhou"
    socio = _socio_pj_controlador(qsa_data)
    if not socio:
        return None, "qsa_sem_socio_pj"
    holding_cnpj, holding_nome = socio
    # 3. Discover domínio do holding via Serper+Hunter (validador 3-guards)
    holding_dominio = discover_domain_via_serper(serper_key, holding_nome, hunter_key)
    if not holding_dominio:
        return None, f"discover_falhou:{holding_nome[:30]}"
    # 4. Persist no cache pra próxima vez
    try:
        cur.execute(
            """UPDATE empresa_dominios
               SET holding_cnpj=%s, holding_nome=%s, holding_dominio=%s,
                   atualizado_em=now()
               WHERE cnpj=%s""",
            (holding_cnpj, holding_nome[:255], holding_dominio, cnpj),
        )
        conn.commit()
    except Exception as e:
        log.warning(f"  persist holding_dominio falhou cnpj={cnpj}: {e}")
        conn.rollback()
    return holding_dominio, f"qsa+discover:{holding_nome[:30]}"


# ───────────────────────── Processamento por obra ──────────────────────────
def processar_obra(cur, conn, obra: dict, hunter_key: str, serper_key: str,
                    args, hunter_saldo_remaining: list[int]) -> dict:
    """Wrapper que garante UPDATE obras.ultimo_enrichment_* em TODOS os paths (incl. early returns)."""
    res = _processar_obra_inner(cur, conn, obra, hunter_key, serper_key, args, hunter_saldo_remaining)
    # v1.4.7: status tracking — chamado uma vez por obra, cobre todos os early returns
    if not args.dry_run:
        _update_obra_status(cur, conn, str(obra["id"]),
                             _classify_status(res.get("decisores", 0), res.get("skip_motivo")),
                             res.get("skip_motivo"))
    return res


def _processar_obra_inner(cur, conn, obra: dict, hunter_key: str, serper_key: str,
                            args, hunter_saldo_remaining: list[int]) -> dict:
    res = {"obra_id": str(obra["id"]), "nome": obra["nome"][:80], "decisores": 0,
           "classificacao_antes": obra["classificacao_computed"],
           "classificacao_depois": None, "skip_motivo": None}

    log.info(f"\n▶ Obra {obra['id']} | prioridade {obra['prioridade']} | "
             f"R${float(obra['valor_estimado'])/1e6:.0f}mi | {obra['empresa']!r}")

    # Passo 2: validar domínio
    dominio, motivo_dom = get_dominio_validado(cur, obra.get("cnpj"))
    if not dominio:
        if getattr(args, 'obra_id', None):
            # Modo admin: discovery reabilitada v1.4.5 com 3 guards (token+blocklist+hunter_confirm)
            log.info(f"  ⚠ domínio não cacheado ({motivo_dom}) — modo admin, discovery via Serper+Hunter")
            dominio = discover_domain_via_serper(serper_key, obra.get("empresa") or "", hunter_key)
            if not dominio:
                log.info(f"  ⊘ discovery falhou (todos candidatos rejeitados pelos 3 guards) — skip Hunter")
                res["skip_motivo"] = "dominio_indescoberto_admin_mode_v1.4.5"
                return res
            log.info(f"  ✓ domínio descoberto via Serper+Hunter: {dominio}")
        else:
            log.info(f"  ⊘ domínio inválido ({motivo_dom}) — skip Hunter")
            res["skip_motivo"] = f"dominio_invalido:{motivo_dom}"
            return res
    else:
        log.info(f"  ✓ domínio validado: {dominio}")

    if args.dry_run:
        log.info(f"  [DRY-RUN] passaria pra Serper LinkedIn (2 calls) + Hunter (até {HUNTER_MAX_CALLS_PER_OBRA})")
        res["skip_motivo"] = "dry_run"
        return res

    # Passo 3: LinkedIn search
    hits = linkedin_search(serper_key, obra["empresa"])
    log.info(f"  Serper LinkedIn: {len(hits)} hits brutos")
    cands = [c for c in (parse_candidato(h, obra["empresa"]) for h in hits) if c]
    cands = rank_candidatos(cands)[:HUNTER_MAX_CALLS_PER_OBRA]
    log.info(f"  candidatos rankeados: {len(cands)}")
    if not cands:
        res["skip_motivo"] = "sem_candidatos_linkedin"
        return res

    # Passo 5: Hunter email-finder (max HUNTER_MAX_CALLS_PER_OBRA)
    inserted = 0
    for cand in cands:
        if hunter_saldo_remaining[0] < 1:
            log.warning(f"  ⚠ saldo Hunter esgotado, parando obra")
            break
        nome_parts = cand["nome"].split()
        first, last = nome_parts[0], " ".join(nome_parts[1:])
        d = hunter_email_finder(hunter_key, dominio, first, last)
        hunter_saldo_remaining[0] -= 1
        email = d.get("email")
        score = int(d.get("score") or 0)
        v_status = (d.get("verification") or {}).get("status")
        if not email or score < 70 or v_status not in ("valid", "accept_all", None):
            log.info(f"  ✗ Hunter {cand['nome']}: email={email} score={score} status={v_status}")
            continue
        if persistir_decisor(cur, obra, cand, email, score, cand["linkedin_url"], None):
            inserted += 1

    # v1.4.7 — fallback holding: Hunter falhou no domínio da subsidiária?
    # Tenta domínio do controlador (BrasilAPI QSA) com os MESMOS candidates.
    # Custo: +1 BrasilAPI (free) + 0-2 Serper (discover, só se sem cache) + até
    # HUNTER_MAX_CALLS_PER_OBRA Hunter. Reuso de candidates evita Serper extra.
    if inserted == 0 and obra.get("cnpj"):
        holding_dom, motivo_hold = get_holding_dominio(cur, conn, obra["cnpj"],
                                                       hunter_key, serper_key)
        if holding_dom and holding_dom != dominio:
            log.info(f"  ↻ fallback holding: {holding_dom} ({motivo_hold})")
            for cand in cands:
                if hunter_saldo_remaining[0] < 1:
                    log.warning(f"  ⚠ saldo Hunter esgotado no fallback, parando")
                    break
                nome_parts = cand["nome"].split()
                first, last = nome_parts[0], " ".join(nome_parts[1:])
                d = hunter_email_finder(hunter_key, holding_dom, first, last)
                hunter_saldo_remaining[0] -= 1
                email = d.get("email")
                score = int(d.get("score") or 0)
                v_status = (d.get("verification") or {}).get("status")
                if not email or score < 70 or v_status not in ("valid", "accept_all", None):
                    log.info(f"  ✗ Hunter[hold] {cand['nome']}: email={email} score={score} status={v_status}")
                    continue
                # extra_componentes audita origem holding pra reviews futuras
                if persistir_decisor(cur, obra, cand, email, score, cand["linkedin_url"], None,
                                      extra_componentes={
                                          "fallback_holding": True,
                                          "holding_dominio": holding_dom,
                                          "holding_motivo": motivo_hold,
                                          "dominio_subsidiaria": dominio,
                                      }):
                    inserted += 1
            if inserted == 0:
                log.info(f"  ⊘ fallback holding também sem aceitos")
        else:
            log.info(f"  ⊘ fallback holding indisponível ({motivo_hold})")

    if inserted == 0:
        res["skip_motivo"] = "nenhum_decisor_persistido"
        return res

    # Passo 6: telefone corporativo (1 call)
    tel = telefone_corporativo(serper_key, obra["empresa"])
    if tel:
        tel_e164 = "+" + re.sub(r"\D", "", tel)
        cur.execute(
            """UPDATE obras SET nivel1_telefone=%s, nivel1_telefone_e164=%s,
               nivel1_telefone_status='ok',
               nivel1_origem_enrichment=%s WHERE id=%s""",
            (tel, tel_e164, f"enrichment_auto:{TODAY_TAG}", obra["id"]),
        )
        log.info(f"  ✓ telefone corporativo: {tel}")

    # Passo 8: recompute
    cur.execute("SELECT recompute_classificacao_obra(%s::uuid)", (obra["id"],))
    cur.execute("SELECT classificacao_computed FROM obras WHERE id=%s", (obra["id"],))
    res["classificacao_depois"] = cur.fetchone()["classificacao_computed"]
    res["decisores"] = inserted
    log.info(f"  ✓ classificacao: {res['classificacao_antes']} → {res['classificacao_depois']}")
    conn.commit()
    return res


# ───────────────────────── Main ────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="Persiste no DB (default dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="Lista obras sem gastar Hunter/Serper")
    ap.add_argument("--limit", type=int, default=MAX_OBRAS_PER_RUN)
    ap.add_argument("--min-capex", type=float, default=CAPEX_MIN)
    ap.add_argument("--obra-id", type=str, default=None,
                    help="UUID de obra específica (modo admin single-obra; bypass filtros + safeguard domínio)")
    ap.add_argument("--admin-bulk", action="store_true",
                    help="Modo bulk admin: bypass filtro criado_em (24h), processa backlog inteiro com filtros de prioridade")
    ap.add_argument("--json-output", action="store_true",
                    help="Imprime RESULT_JSON:{...} na última linha pra parsing programático")
    ap.add_argument("--cascade-admin", action="store_true",
                    help="Modo cascata botão Enriquecer: 5 gaps (CNPJ→domínio→106 cargos→email Hunter+pattern→telefone multi-source). Requer --obra-id.")
    args = ap.parse_args()

    if not args.commit and not args.dry_run:
        args.dry_run = True  # default

    hunter_key = os.environ.get("HUNTER_API_KEY", "").strip()
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not hunter_key or not serper_key:
        log.error("HUNTER_API_KEY ou SERPER_API_KEY ausente")
        sys.exit(2)

    # v1.4.8: dispatch cascade admin antes do fluxo legado
    if args.cascade_admin:
        if not args.obra_id:
            log.error("--cascade-admin requer --obra-id")
            sys.exit(2)
        cascade_main_dispatch(args, hunter_key, serper_key)
        return

    if not args.dry_run:
        saldo = hunter_saldo(hunter_key)
        log.info(f"Hunter saldo: {saldo} (mínimo {HUNTER_MIN_SALDO})")
        if saldo < HUNTER_MIN_SALDO:
            log.error(f"Hunter saldo {saldo} < {HUNTER_MIN_SALDO}; abortando")
            sys.exit(3)
        hunter_saldo_remaining = [saldo]
    else:
        hunter_saldo_remaining = [9999]

    conn = psycopg2.connect(cursor_factory=RealDictCursor, **DB_CONFIG)
    cur = conn.cursor()
    if args.obra_id:
        cur.execute(SQL_OBRA_ID_SINGLE, (args.obra_id,))
        obras = cur.fetchall()
        log.info(f"Modo single-obra (admin): obra_id={args.obra_id} → {len(obras)} match")
    else:
        sql_q = _build_sql_obras_prioridade(admin_bulk=args.admin_bulk)
        cur.execute(sql_q, (args.min_capex, f"enrichment_auto:v1:{TODAY_TAG}%", args.limit))
        obras = cur.fetchall()
        modo = "admin-bulk" if args.admin_bulk else "cron-24h"
        log.info(f"Obras candidatas ({modo}): {len(obras)} (limit={args.limit}, min_capex={args.min_capex:.0f})")

    if not obras:
        log.info("Nenhuma obra qualifica. Encerrando.")
        if args.json_output:
            print(f"RESULT_JSON: {json.dumps({'decisores_inseridos':0,'obras_processadas':0,'obras_promovidas':0,'tier_antes':None,'tier_depois':None,'skip_motivo':'obra_nao_encontrada' if args.obra_id else 'nenhuma_obra_qualifica','marker':MARKER})}", flush=True)
        return

    if args.dry_run:
        log.info("─── DRY-RUN: obras que SERIAM processadas ───")
        for o in obras:
            log.info(
                f"  P{o['prioridade']} | R${float(o['valor_estimado'])/1e6:>7.0f}mi | "
                f"cnpj={(o.get('cnpj') or '?'):>15} | {(o.get('empresa') or '?')[:50]!s}"
            )

    results = []
    for obra in obras:
        try:
            r = processar_obra(cur, conn, obra, hunter_key, serper_key,
                                args, hunter_saldo_remaining)
            results.append(r)
        except Exception as e:
            log.exception(f"Erro processando obra {obra['id']}: {e}")
            conn.rollback()

    promovidas = sum(1 for r in results if r.get("classificacao_depois")
                                            and r["classificacao_depois"] != r["classificacao_antes"])
    log.info(f"\n══ SUMÁRIO ══")
    log.info(f"  Obras processadas: {len(results)}")
    log.info(f"  Decisores inseridos: {sum(r['decisores'] for r in results)}")
    log.info(f"  Obras promovidas: {promovidas}")
    log.info(f"  Hunter usado: {hunter_saldo_remaining[0] if args.dry_run else 'n/a'}")
    log.info(f"  Marker: {MARKER}")

    if args.json_output:
        first = results[0] if results else {}
        summary = {
            "decisores_inseridos": sum(r.get('decisores', 0) for r in results),
            "obras_processadas": len(results),
            "obras_promovidas": promovidas,
            "tier_antes": first.get('classificacao_antes'),
            "tier_depois": first.get('classificacao_depois'),
            "skip_motivo": first.get('skip_motivo'),
            "hunter_saldo_fim": hunter_saldo_remaining[0] if not args.dry_run else None,
            "marker": MARKER,
        }
        print(f"RESULT_JSON: {json.dumps(summary)}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# CASCADE ADMIN MODE v1.4.8 — botão "Enriquecer" do dashboard
# Cobre 5 gaps: CNPJ resolver → persist domínio → 106 cargos Mari →
#               email Hunter+pattern → telefone multi-source.
# Dispatch via --cascade-admin. NUNCA chamado pelo cron — cron usa fluxo legado.
# ═══════════════════════════════════════════════════════════════════════════

CASCADE_HUNTER_MAX = 6        # 5 email-finder + 1 domain-search
CASCADE_SERPER_MAX = 35       # 30 LinkedIn (Mari 11 buckets ~22 calls) + 5 telefone/domínio
CASCADE_HUNTER_MIN = 50       # mesmo mínimo do cron


class CascadeBudget:
    """Tracker de quota por execução de cascata (1 obra)."""
    def __init__(self, hunter_saldo_inicial: int):
        self.hunter_calls = 0
        self.serper_calls = 0
        self.hunter_saldo_inicial = hunter_saldo_inicial

    def pode_hunter(self) -> bool:
        # respeita o cap por-obra E o saldo real da conta (11/06: antes ignorava
        # saldo, e o único freio era o gate do entry-point que abortava tudo).
        return (self.hunter_calls < CASCADE_HUNTER_MAX
                and self.hunter_saldo_inicial >= CASCADE_HUNTER_MIN)

    def pode_serper(self, n: int = 1) -> bool:
        return (self.serper_calls + n) <= CASCADE_SERPER_MAX


def cascade_log_passo(cur, conn, obra_id: str, passo: str, status: str, detalhe: str = ""):
    """Append linha [ts] PASSO: STATUS detalhe em obras.observacoes_enrichment."""
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {passo}: {status}"
    if detalhe:
        line += f" — {detalhe[:300]}"
    try:
        cur.execute(
            "UPDATE obras SET observacoes_enrichment = COALESCE(observacoes_enrichment,'') || %s || E'\\n' WHERE id = %s::uuid",
            (line, obra_id),
        )
        conn.commit()
    except Exception as e:
        log.warning(f"cascade_log_passo falhou: {e}")
        conn.rollback()


def cascade_resolver_cnpj(cur, empresa: str) -> tuple[str | None, str]:
    """PASSO 0: resolve CNPJ via fornecedores.razao_social/nome_fantasia.
    Retorna (cnpj, fonte) — None se ambíguo ou não encontrado."""
    if not empresa or not empresa.strip():
        return None, "sem_empresa"
    emp = empresa.strip()
    for col in ("razao_social", "nome_fantasia"):
        cur.execute(
            f"SELECT cnpj FROM fornecedores WHERE {col} ILIKE %s AND situacao='ATIVA' LIMIT 2",
            (emp,),
        )
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0]["cnpj"], f"fornecedores.{col}"
        if len(rows) > 1:
            return None, f"ambiguo_{col}"
    # tentativa com prefixo (até 1ª vírgula ou /)
    short = re.split(r"[,\/]", emp, 1)[0].strip()
    if short and short != emp and len(short) >= 6:
        cur.execute(
            "SELECT cnpj FROM fornecedores WHERE razao_social ILIKE %s AND situacao='ATIVA' LIMIT 2",
            (short + "%",),
        )
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0]["cnpj"], "fornecedores.razao_social.prefix"
    return None, "nao_encontrado"


def cascade_persistir_dominio(cur, conn, cnpj: str, dominio: str, marker: str):
    """PASSO 1: persiste domínio descoberto em empresa_dominios (idempotente)."""
    obs = f"enriquecer_botao_{marker}"
    try:
        cur.execute(
            """INSERT INTO empresa_dominios (cnpj, dominio, confianca, validacao_metodo, observacoes, atualizado_em)
               VALUES (%s, %s, 5, 'admin_cascade_v1', %s, now())
               ON CONFLICT (cnpj) DO UPDATE
                 SET dominio = COALESCE(empresa_dominios.dominio, EXCLUDED.dominio),
                     observacoes = COALESCE(empresa_dominios.observacoes,'') || ' | ' || EXCLUDED.observacoes,
                     atualizado_em = now()
               WHERE empresa_dominios.dominio IS NULL""",
            (cnpj, dominio, obs),
        )
        conn.commit()
    except Exception as e:
        log.warning(f"cascade_persistir_dominio falhou cnpj={cnpj}: {e}")
        conn.rollback()


def cascade_tecnica_mari(empresa: str, cnpj: str | None, budget: CascadeBudget,
                          max_buckets: int = 11) -> tuple[list, int]:
    """PASSO 2: 106 cargos via descobrir_via_search_engines (bucket1.5 stack).
    Retorna (lista DecisorBruto, n_buckets_rodados). NÃO toca em bucket1.5."""
    if not empresa:
        return [], 0
    # cada bucket = 1 query Serper; respeita cap CASCADE_SERPER_MAX restante
    available = CASCADE_SERPER_MAX - budget.serper_calls - 5  # reserva 5 pra telefone/discover
    n_buckets = max(1, min(max_buckets, available))
    try:
        from sales_intelligence.camada3_decisores.linkedin_search import descobrir_via_search_engines
        decisores = descobrir_via_search_engines(empresa, cnpj=cnpj, max_buckets=n_buckets)
        budget.serper_calls += n_buckets  # contagem conservadora
        return decisores or [], n_buckets
    except Exception as e:
        log.warning(f"cascade_tecnica_mari falhou empresa={empresa!r}: {e}")
        return [], 0


def cascade_email_pattern_guess(cur, dominio: str, first: str, last: str) -> str | None:
    """PASSO 3 fallback A: consulta empresa_email_pattern_cache."""
    cur.execute("SELECT padrao FROM empresa_email_pattern_cache WHERE dominio=%s LIMIT 1", (dominio,))
    row = cur.fetchone()
    if not row or not row.get("padrao"):
        return None
    padrao = row["padrao"]
    from unidecode import unidecode
    f_norm = re.sub(r"[^a-z]", "", unidecode(first.lower().strip()))
    l_norm = re.sub(r"[^a-z]", "", unidecode(last.lower().strip().split()[-1]))  # último sobrenome só
    if not f_norm or not l_norm:
        return None
    email = (padrao
             .replace("{first}", f_norm)
             .replace("{last}", l_norm)
             .replace("{f}", f_norm[:1])
             .replace("{l}", l_norm[:1]))
    if "@" not in email:
        email = f"{email}@{dominio}"
    return email


def cascade_aprender_pattern(cur, conn, hunter_key: str, dominio: str,
                              budget: CascadeBudget) -> str | None:
    """PASSO 3 fallback B: 1 Hunter /domain-search descobre pattern, salva no cache."""
    if not budget.pode_hunter():
        return None
    try:
        qs = urllib.parse.urlencode({"domain": dominio, "limit": 10})
        req = urllib.request.Request(
            f"https://api.hunter.io/v2/domain-search?{qs}",
            headers={"User-Agent": UA, "Authorization": f"Bearer {hunter_key}"},
        )
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        budget.hunter_calls += 1
        data = r.get("data") or {}
        padrao = data.get("pattern")
        emails = data.get("emails") or []
        if not padrao or not emails:
            return None
        try:
            cur.execute(
                """INSERT INTO empresa_email_pattern_cache
                   (dominio, padrao, confianca, exemplos, amostra_total, detectado_em, pessoas_count)
                   VALUES (%s, %s, 'media', %s::jsonb, %s, now(), %s)
                   ON CONFLICT (dominio) DO UPDATE
                     SET padrao = COALESCE(empresa_email_pattern_cache.padrao, EXCLUDED.padrao),
                         pessoas_count = GREATEST(empresa_email_pattern_cache.pessoas_count, EXCLUDED.pessoas_count)""",
                (dominio, padrao, Json([e.get("value") for e in emails[:3]]),
                 len(emails), len(emails)),
            )
            conn.commit()
        except Exception as e:
            log.warning(f"cascade_aprender_pattern persist falhou: {e}")
            conn.rollback()
        return padrao
    except Exception as e:
        log.warning(f"cascade_aprender_pattern hunter falhou {dominio}: {e}")
        return None


def cascade_telefone(cur, empresa: str, cnpj: str | None, serper_key: str,
                      budget: CascadeBudget) -> tuple[str | None, str | None]:
    """PASSO 4: telefone via BrasilAPI → fornecedores → Serper."""
    # a) BrasilAPI (free, ddd_telefone_1 + telefone_1)
    if cnpj:
        qsa = _brasilapi_qsa(cnpj)
        if qsa:
            ddd = qsa.get("ddd_telefone_1") or ""
            tel = qsa.get("telefone_1") or ""
            digits = re.sub(r"\D", "", str(ddd) + str(tel))
            if len(digits) >= 10:
                if not digits.startswith("55"):
                    digits = "55" + digits
                return "+" + digits, "brasilapi"
    # b) fornecedores.telefone_1 via CNPJ
    if cnpj:
        try:
            cur.execute("SELECT telefone_1 FROM fornecedores WHERE cnpj=%s", (cnpj,))
            row = cur.fetchone()
            if row and row.get("telefone_1"):
                digits = re.sub(r"\D", "", row["telefone_1"])
                if len(digits) >= 10:
                    if not digits.startswith("55"):
                        digits = "55" + digits
                    return "+" + digits, "rfb_telefone_1"
        except Exception as e:
            log.warning(f"cascade_telefone fornecedores falhou: {e}")
    # c) Serper fallback
    if empresa and budget.pode_serper(1):
        budget.serper_calls += 1
        tel = telefone_corporativo(serper_key, empresa)
        if tel:
            return tel, "serper"
    return None, None


_CARGOS_DECISORES_PRIO = {
    "SUPPLY_CHAIN", "GERENTE_SUPRIMENTOS", "GERENTE_COMPRAS",
    "GERENTE_PROJETOS", "GERENTE_ENGENHARIA", "GERENTE_INDUSTRIAL",
    "COORDENADOR_OBRAS", "COORDENADOR_MANUTENCAO",
    "ENGENHEIRO_MECANICO_CIVIL", "PROJETISTA",
}


def cascade_obra_admin(cur, conn, obra: dict, hunter_key: str, serper_key: str,
                        hunter_saldo_inicial: int) -> dict:
    """Orquestrador. NUNCA aborta cascata por falha intermediária — só por hard fail (sem CNPJ)."""
    obra_id = str(obra["id"])
    budget = CascadeBudget(hunter_saldo_inicial)
    res = {
        "obra_id": obra_id,
        "nome": (obra.get("nome") or "")[:80],
        "empresa": obra.get("empresa"),
        "classificacao_antes": obra.get("classificacao_computed"),
        "classificacao_depois": None,
        "passos": {},
        "decisores_inseridos": 0,
        "hunter_calls": 0,
        "serper_calls": 0,
        "erro": None,
    }
    cnpj = obra.get("cnpj")
    empresa = (obra.get("empresa") or "").strip()

    # ─── PASSO 0: CNPJ ──────────────────────────────────────────────────────
    if not cnpj:
        if not empresa:
            cascade_log_passo(cur, conn, obra_id, "PASSO_0_CNPJ", "SKIP", "sem_empresa_sem_cnpj")
            res["passos"]["0_cnpj"] = {"status": "fail", "motivo": "sem_empresa_sem_cnpj"}
            res["erro"] = "Sem CNPJ"
            return res
        novo_cnpj, fonte = cascade_resolver_cnpj(cur, empresa)
        if novo_cnpj:
            try:
                cur.execute("UPDATE obras SET cnpj=%s WHERE id=%s::uuid", (novo_cnpj, obra_id))
                conn.commit()
                cnpj = novo_cnpj
                cascade_log_passo(cur, conn, obra_id, "PASSO_0_CNPJ", "OK", f"{cnpj} via {fonte}")
                res["passos"]["0_cnpj"] = {"status": "ok", "cnpj": cnpj, "fonte": fonte}
            except Exception as e:
                conn.rollback()
                cascade_log_passo(cur, conn, obra_id, "PASSO_0_CNPJ", "FAIL", f"update_erro:{e}")
                res["passos"]["0_cnpj"] = {"status": "fail", "motivo": "update_erro"}
                res["erro"] = "Sem CNPJ"
                return res
        else:
            cascade_log_passo(cur, conn, obra_id, "PASSO_0_CNPJ", "FAIL", fonte)
            res["passos"]["0_cnpj"] = {"status": "fail", "motivo": fonte}
            res["erro"] = "Sem CNPJ"
            return res
    else:
        res["passos"]["0_cnpj"] = {"status": "ok", "cnpj": cnpj, "fonte": "obra"}

    # ─── PASSO 1: DOMÍNIO (cache → discover Serper+Hunter → persist) ────────
    dominio, motivo_dom = get_dominio_validado(cur, cnpj)
    if not dominio:
        if budget.pode_serper(1):
            budget.serper_calls += 1
            try:
                if budget.pode_hunter():
                    dominio = discover_domain_via_serper(serper_key, empresa, hunter_key)
                    if dominio:
                        budget.hunter_calls += 1  # discover usa 1 Hunter domain-search
                else:
                    dominio = discover_domain_via_serper(serper_key, empresa, None)
            except Exception as e:
                log.warning(f"discover_domain crash: {e}")
                dominio = None
            if dominio:
                cascade_persistir_dominio(cur, conn, cnpj, dominio, MARKER)
                cascade_log_passo(cur, conn, obra_id, "PASSO_1_DOMINIO", "DESCOBERTO", dominio)
                res["passos"]["1_dominio"] = {"status": "descoberto", "dominio": dominio}
            else:
                cascade_log_passo(cur, conn, obra_id, "PASSO_1_DOMINIO", "FAIL", motivo_dom)
                res["passos"]["1_dominio"] = {"status": "fail", "motivo": motivo_dom}
        else:
            cascade_log_passo(cur, conn, obra_id, "PASSO_1_DOMINIO", "SKIP", "serper_cap_atingido")
            res["passos"]["1_dominio"] = {"status": "skip", "motivo": "serper_cap"}
    else:
        cascade_log_passo(cur, conn, obra_id, "PASSO_1_DOMINIO", "CACHE_HIT", dominio)
        res["passos"]["1_dominio"] = {"status": "cache_hit", "dominio": dominio}

    # ─── PASSO 2: LINKEDIN 106 cargos (descobrir_via_search_engines) ────────
    candidatos: list = []
    if empresa:
        decisores, n_buckets = cascade_tecnica_mari(empresa, cnpj, budget, max_buckets=11)
        candidatos = decisores
        cascade_log_passo(cur, conn, obra_id, "PASSO_2_LINKEDIN", "OK",
                           f"{len(candidatos)} candidatos | buckets={n_buckets} | serper_acum={budget.serper_calls}")
        # v1.5.0: gate evidencia nome-so — filtra ANTES dos 3 caminhos de persist
        # (email-finder, degradacao hunter, sem-dominio). So INSERTs novos.
        aprovados = []
        for c in candidatos:
            if _cand_tem_evidencia(c, empresa):
                aprovados.append(c)
            else:
                cascade_log_passo(cur, conn, obra_id, "PASSO_2_LINKEDIN", "GATE_EVIDENCIA",
                                   f"{c.nome_pessoa}: nome-so sem vinculo (conf={getattr(c, 'confianca', '?')})")
        bloqueados = len(candidatos) - len(aprovados)
        candidatos = aprovados
        res["passos"]["2_linkedin"] = {"status": "ok", "candidatos": len(candidatos),
                                         "buckets_rodados": n_buckets,
                                         "gate_evidencia_bloqueados": bloqueados}
    else:
        cascade_log_passo(cur, conn, obra_id, "PASSO_2_LINKEDIN", "SKIP", "sem_empresa")
        res["passos"]["2_linkedin"] = {"status": "skip", "motivo": "sem_empresa"}

    # ─── BACKFILL: completa email de decisores email_pendente (PRATA→OURO) ──
    # Roda independente de novos candidatos — só precisa de domínio + Hunter.
    if dominio and budget.pode_hunter():
        bf = cascade_backfill_emails_pendentes(cur, conn, obra, dominio, hunter_key, budget)
        if bf:
            cascade_log_passo(cur, conn, obra_id, "PASSO_3_BACKFILL", "OK", f"{bf} emails backfilled")
            res["passos"]["3_backfill"] = {"status": "ok", "backfilled": bf}

    # ─── PASSO 3: EMAIL (Hunter cap 5 email-finder + pattern fallback) ──────
    if dominio and candidatos:
        # Prioriza decisores reais (mesma lista do bucket1.5)
        prio = [c for c in candidatos if (getattr(c, "tipo_cargo", "") or "") in _CARGOS_DECISORES_PRIO]
        resto = [c for c in candidatos if (getattr(c, "tipo_cargo", "") or "") not in _CARGOS_DECISORES_PRIO]
        ordenados = prio + resto
        inseridos = 0
        pattern_aprendido = False
        hunter_email_finder_used = 0
        # nomes já decisores da obra (inclui os backfilled acima) → não re-gastar Hunter
        cur.execute("SELECT lower(nome) AS n FROM decisores_obra "
                    "WHERE obra_id=%s::uuid AND excluido_em IS NULL", (obra_id,))
        _ja_decisores = {r["n"] for r in cur.fetchall()}
        for cand in ordenados:
            # cap 5 email-finder (deixa 1 Hunter pra domain-search se precisar aprender pattern)
            if hunter_email_finder_used >= 5:
                break
            if not budget.pode_hunter():
                break
            if (cand.nome_pessoa or "").strip().lower() in _ja_decisores:
                continue  # já é decisor (ex.: backfilled) — evita gasto Hunter redundante
            nome_partes = (cand.nome_pessoa or "").split()
            if len(nome_partes) < 2:
                continue
            first, last = nome_partes[0], " ".join(nome_partes[1:])
            d = hunter_email_finder(hunter_key, dominio, first, last)
            budget.hunter_calls += 1
            hunter_email_finder_used += 1
            email = (d or {}).get("email")
            score = int((d or {}).get("score") or 0)
            v_status = ((d or {}).get("verification") or {}).get("status")
            email_status = "hunter_verified"
            if not email or score < 70 or v_status not in ("valid", "accept_all", None):
                guess = cascade_email_pattern_guess(cur, dominio, first, last)
                if not guess and not pattern_aprendido and budget.pode_hunter():
                    learned = cascade_aprender_pattern(cur, conn, hunter_key, dominio, budget)
                    pattern_aprendido = True
                    if learned:
                        guess = cascade_email_pattern_guess(cur, dominio, first, last)
                if guess:
                    email = guess
                    email_status = "pattern_guess"
                    score = 50
                else:
                    continue
            # v1.4.9: gate pessoa-vs-setor (camada2 classificar_pessoa) — local-part
            # setorial/placeholder (compras@, contato@, noreply@...) nunca e decisor.
            # So bloqueia INSERT novo; decisores ja persistidos nao sao tocados.
            lp_tipo = classificar_local_part(email.split("@", 1)[0])
            if lp_tipo in ("setor", "placeholder"):
                cascade_log_passo(cur, conn, obra_id, "PASSO_3_EMAIL", "GATE_PESSOA_VS_SETOR",
                                   f"{cand.nome_pessoa}: {email} local_part={lp_tipo}")
                continue
            # construir cand dict no formato esperado por persistir_decisor
            cand_dict = {
                "nome": cand.nome_pessoa,
                "cargo": cand.cargo_raw or "",
                "linkedin_url": (f"https://br.linkedin.com/in/{cand.linkedin_slug}"
                                  if getattr(cand, "linkedin_slug", None) else ""),
                "raw_title": "",
                "raw_snippet": getattr(cand, "snippet_origem", "") or "",
            }
            extra = {
                "fonte_pipeline": "cascade_admin_v1.4.8",
                "email_status": email_status,
                "cargo_raw_original": cand.cargo_raw,
                "tipo_cargo_mari": getattr(cand, "tipo_cargo", None),
                "fonte_descoberta_mari": getattr(cand, "fonte_descoberta", None),
            }
            if persistir_decisor(cur, obra, cand_dict, email, score,
                                  cand_dict["linkedin_url"], None,
                                  extra_componentes=extra):
                inseridos += 1
        # Degradação graciosa: Hunter indisponível (saldo baixo) e 0 emails →
        # persiste leads do LinkedIn SEM email (email_pendente → PRATA). Cap 2/obra.
        if inseridos == 0 and not budget.pode_hunter():
            for cand in ordenados[:2]:
                if _persistir_lead_sem_email(cur, obra, cand):
                    inseridos += 1
        res["decisores_inseridos"] = inseridos
        cascade_log_passo(cur, conn, obra_id, "PASSO_3_EMAIL", "OK",
                           f"{inseridos} inseridos | hunter_acum={budget.hunter_calls}")
        res["passos"]["3_email"] = {"status": "ok", "inseridos": inseridos,
                                      "hunter_email_finder_calls": hunter_email_finder_used,
                                      "pattern_aprendido": pattern_aprendido}
    elif candidatos:
        # Sem domínio (Hunter indisponível p/ discovery) mas LinkedIn achou leads →
        # persiste SEM email (email_pendente → PRATA), em vez de perder o lead.
        inseridos = 0
        for cand in candidatos[:2]:
            if _persistir_lead_sem_email(cur, obra, cand):
                inseridos += 1
        res["decisores_inseridos"] = inseridos
        cascade_log_passo(cur, conn, obra_id, "PASSO_3_EMAIL", "OK",
                           f"{inseridos} email_pendente (sem dominio/hunter)")
        res["passos"]["3_email"] = {"status": "ok_email_pendente", "inseridos": inseridos}
    else:
        cascade_log_passo(cur, conn, obra_id, "PASSO_3_EMAIL", "SKIP", "sem_candidatos")
        res["passos"]["3_email"] = {"status": "skip", "motivo": "sem_candidatos"}

    # ─── PASSO 4: TELEFONE multi-source ─────────────────────────────────────
    tel, fonte_tel = cascade_telefone(cur, empresa, cnpj, serper_key, budget)
    if tel:
        try:
            cur.execute(
                """UPDATE obras SET nivel1_telefone=%s, nivel1_telefone_e164=%s,
                       nivel1_telefone_status='ok',
                       nivel1_origem_enrichment=%s
                   WHERE id=%s::uuid""",
                (tel, tel, f"cascade_admin:{fonte_tel}:{TODAY_TAG}", obra_id),
            )
            conn.commit()
            cascade_log_passo(cur, conn, obra_id, "PASSO_4_TELEFONE", "OK", f"{tel} | fonte={fonte_tel}")
            res["passos"]["4_telefone"] = {"status": "ok", "telefone": tel, "fonte": fonte_tel}
        except Exception as e:
            conn.rollback()
            cascade_log_passo(cur, conn, obra_id, "PASSO_4_TELEFONE", "FAIL", f"update_erro:{e}")
            res["passos"]["4_telefone"] = {"status": "fail", "motivo": "update_erro"}
    else:
        cascade_log_passo(cur, conn, obra_id, "PASSO_4_TELEFONE", "FAIL", "nenhuma_fonte")
        res["passos"]["4_telefone"] = {"status": "fail", "motivo": "nenhuma_fonte"}

    # ─── PASSO 5: recompute classificacao ───────────────────────────────────
    try:
        cur.execute("SELECT recompute_classificacao_obra(%s::uuid)", (obra_id,))
        cur.execute("SELECT classificacao_computed FROM obras WHERE id=%s::uuid", (obra_id,))
        row = cur.fetchone()
        res["classificacao_depois"] = row["classificacao_computed"] if row else None
        conn.commit()
        cascade_log_passo(cur, conn, obra_id, "PASSO_5_RECOMPUTE", "OK",
                           f"{res['classificacao_antes']} → {res['classificacao_depois']}")
    except Exception as e:
        conn.rollback()
        cascade_log_passo(cur, conn, obra_id, "PASSO_5_RECOMPUTE", "FAIL", str(e)[:100])

    res["hunter_calls"] = budget.hunter_calls
    res["serper_calls"] = budget.serper_calls
    return res


def cascade_main_dispatch(args, hunter_key: str, serper_key: str):
    """Entry point quando main() detecta --cascade-admin + --obra-id."""
    # quota check (11/06: NÃO aborta mais. Com Hunter esgotado o cascade roda
    # CNPJ→domínio-cacheado→LinkedIn→telefone e persiste o decisor do LinkedIn SEM
    # email — email_pendente → PRATA. Backfill de email quando a quota Hunter voltar.)
    saldo = hunter_saldo(hunter_key)
    hunter_disponivel = saldo >= CASCADE_HUNTER_MIN
    log.info(f"Cascade admin — Hunter saldo: {saldo} (min {CASCADE_HUNTER_MIN}) "
             f"disponivel={hunter_disponivel}")

    conn = psycopg2.connect(cursor_factory=RealDictCursor, **DB_CONFIG)
    cur = conn.cursor()
    try:
        cur.execute(SQL_OBRA_ID_SINGLE, (args.obra_id,))
        obras = cur.fetchall()
        if not obras:
            out = {"erro": "obra_nao_encontrada", "obra_id": args.obra_id}
            if args.json_output:
                print(f"RESULT_JSON: {json.dumps(out)}", flush=True)
            return
        r = cascade_obra_admin(cur, conn, obras[0], hunter_key, serper_key, saldo)
        r["hunter_saldo_inicio"] = saldo
        r["hunter_saldo_estimado_fim"] = saldo - r.get("hunter_calls", 0)
        r["hunter_disponivel"] = hunter_disponivel
        r["email_pendente"] = (not hunter_disponivel) and r.get("decisores_inseridos", 0) > 0
        r["marker"] = MARKER
        if args.json_output:
            print(f"RESULT_JSON: {json.dumps(r, default=str)}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("enrichment_auto")
    main()
else:
    log = logging.getLogger("enrichment_auto")
