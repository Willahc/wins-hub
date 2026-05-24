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

# ───────────────────────── Safeguards / constantes ─────────────────────────
MAX_OBRAS_PER_RUN = 10
HUNTER_MIN_SALDO = 50
HUNTER_MAX_CALLS_PER_OBRA = 4
SERPER_LINKEDIN_CALLS = 2
SERPER_MARI_CALLS = 4
SERPER_TELEFONE_CALLS = 1
CAPEX_MIN = 50_000_000  # ignora PNCP pequenos
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0 Safari/537.36"
TODAY_TAG = datetime.now().strftime("%Y%m%d")
MARKER = f"enrichment_auto:v1:{TODAY_TAG}"

# Tiers de prioridade
SQL_OBRAS_PRIORIDADE = """
WITH cand AS (
  SELECT
    o.id, o.nome, o.empresa, o.cnpj, o.setor, o.uf,
    o.valor_estimado, o.fonte, o.fonte_tipo,
    o.classificacao_computed, o.criado_em,
    CASE
      WHEN o.valor_estimado >= 1e9
           AND COALESCE(o.fonte_tipo,'OFICIAL') = 'OFICIAL' THEN 1
      WHEN o.valor_estimado >= 500e6
           AND COALESCE(o.fonte_tipo,'OFICIAL') = 'OFICIAL' THEN 2
      WHEN o.valor_estimado >= 100e6
           AND COALESCE(o.fonte_tipo,'OFICIAL') IN ('MANUAL','PESQUISA_MANUAL') THEN 3
      ELSE NULL
    END AS prioridade
  FROM obras o
  WHERE o.criado_em >= now() - interval '24 hours'
    AND o.valor_estimado >= %s
    AND o.nivel1_nome IS NULL
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
WHERE prioridade IS NOT NULL
ORDER BY prioridade, valor_estimado DESC NULLS LAST
LIMIT %s;
"""

# ───────────────────────── Hunter helpers ──────────────────────────────────
def hunter_saldo(api_key: str) -> int:
    req = urllib.request.Request(
        f"https://api.hunter.io/v2/account?api_key={api_key}",
        headers={"User-Agent": UA},
    )
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    r = d.get("data", {}).get("requests", {}).get("searches", {})
    return int(r.get("available", 0)) - int(r.get("used", 0))


def hunter_email_finder(api_key: str, domain: str, first: str, last: str) -> dict:
    qs = urllib.parse.urlencode(
        {"domain": domain, "first_name": first, "last_name": last, "api_key": api_key}
    )
    req = urllib.request.Request(
        f"https://api.hunter.io/v2/email-finder?{qs}", headers={"User-Agent": UA}
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
    # cargo: combina parts[1] + snippet pra detectar áreas
    cargo = parts[1]
    # exige empresa correta
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
                       linkedin_url: str, telefone: str | None) -> bool:
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
    confianca = max(70, min(score, 97))
    cur.execute(
        """INSERT INTO decisores_obra
           (obra_id, nome, cargo, linkedin_url, email, telefone,
            fonte, registrado_por, tipo_cargo, confianca_match,
            hipotese_replicacao, confianca_match_componentes,
            confianca_match_calculada_em)
           VALUES (%s,%s,%s,%s,%s,%s,
                   'serper_linkedin+hunter_email_finder', %s, %s, %s,
                   NULL, %s, now())
           ON CONFLICT (obra_id, nome) WHERE excluido_em IS NULL DO NOTHING
           RETURNING id""",
        (obra["id"], nome, cargo, linkedin_url, email, telefone,
         MARKER, tipo, confianca, Json(componentes)),
    )
    row = cur.fetchone()
    if row:
        log.info(f"  ✓ INSERT decisor {nome} ({cargo}) email={email} score={score}")
        return True
    log.info(f"  ⊘ skip decisor {nome}: já existe (ON CONFLICT)")
    return False


# ───────────────────────── Single-obra mode (admin button) ────────────────
SQL_OBRA_ID_SINGLE = """
SELECT
  o.id, o.nome, o.empresa, o.cnpj, o.setor, o.uf,
  o.valor_estimado, o.fonte, o.fonte_tipo,
  o.classificacao_computed, o.criado_em,
  99 AS prioridade
FROM obras o WHERE o.id = %s::uuid
"""


def discover_domain_via_serper(serper_key: str, empresa: str) -> str | None:
    """Fallback pra modo admin: descobre domínio canônico via Serper se empresa_dominios vazio."""
    if not empresa:
        return None
    hits = serper_search(serper_key, f'"{empresa}" site oficial OR "fale conosco"', num=5)
    counts: dict[str, int] = {}
    for h in hits:
        url = h.get('link') or ''
        m = re.search(r'https?://(?:www\.)?([^/]+)', url)
        if not m:
            continue
        d = m.group(1).lower()
        if any(s in d for s in ('linkedin', 'facebook', 'instagram', 'twitter', 'wikipedia', 'youtube', 'gov.br')):
            continue
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda x: x[1])[0]


# ───────────────────────── Processamento por obra ──────────────────────────
def processar_obra(cur, conn, obra: dict, hunter_key: str, serper_key: str,
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
            # Modo admin: bypass safeguard, tenta descobrir via Serper
            log.info(f"  ⚠ domínio não cacheado/inválido ({motivo_dom}) — modo admin, tentando discovery via Serper")
            dominio = discover_domain_via_serper(serper_key, obra.get("empresa") or "")
            if not dominio:
                log.info(f"  ⊘ discovery via Serper falhou — skip Hunter")
                res["skip_motivo"] = "dominio_indescoberto_admin_mode"
                return res
            log.info(f"  ✓ domínio descoberto via Serper: {dominio}")
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
    ap.add_argument("--json-output", action="store_true",
                    help="Imprime RESULT_JSON:{...} na última linha pra parsing programático")
    args = ap.parse_args()

    if not args.commit and not args.dry_run:
        args.dry_run = True  # default

    hunter_key = os.environ.get("HUNTER_API_KEY", "").strip()
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not hunter_key or not serper_key:
        log.error("HUNTER_API_KEY ou SERPER_API_KEY ausente")
        sys.exit(2)

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
        cur.execute(SQL_OBRAS_PRIORIDADE,
                    (args.min_capex, f"enrichment_auto:v1:{TODAY_TAG}%", args.limit))
        obras = cur.fetchall()
        log.info(f"Obras candidatas: {len(obras)} (limit={args.limit}, min_capex={args.min_capex:.0f})")

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
