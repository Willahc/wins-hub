#!/usr/bin/env python3
"""Captador industrial privado — Estágios 1 (descoberta) + 2 (processamento).

Fecha lacuna estrutural diagnosticada 31/05: setores AUTOMOTIVO / ALIMENTOS / QUIMICA
/ TECH / ELETRO têm cobertura 100% por notícia/manual (15 obras AUTOMOTIVO totais,
0 de captador estrutural). Investimento industrial privado não passa por licitação
pública — só entra hoje por link manual (XBRI, Guofuhee).

ESTAGIO 1 (--descoberta): enfileira sinais brutos em candidatos_industrial,
status='novo'. NÃO cadastra obra, NÃO chama Hunter.

ESTAGIO 2 (--processar): valida candidatos via Haiku, cross-dedup vs obras, INSERT
em obras + recompute_classificacao_obra. NÃO chama Hunter. BrasilAPI step é
forward-compatible (candidatos_industrial não armazena CNPJ ainda).

Uso:
    python /app/scripts/captar_industrial_priv.py --descoberta             # commit
    python /app/scripts/captar_industrial_priv.py --descoberta --dry-run   # só lista
    python /app/scripts/captar_industrial_priv.py --processar --dry-run    # valida sem inserir
    python /app/scripts/captar_industrial_priv.py --processar --limite 50  # commit
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
import psycopg2
import requests

LOG_DIR = Path("/app/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"captar_industrial_priv_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, mode="a"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("captar_industrial_priv")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SERPER_URL = "https://google.serper.dev/news"
SERPER_TIMEOUT = 20
RESULTS_PER_QUERY = 10

# v1: queries reformuladas pós-validação humana (31/05).
# setor_alvo aqui é só hint do que a query busca; setor real vem do Haiku (FIX 1).
QUERIES = [
    {"q": '("nova fábrica" OR "nova planta") (montadora OR pneu OR autopeças OR automotivo) Brasil investimento', "hint": "automotivo"},
    {"q": '(XBRI OR Linglong OR Continental OR Pirelli OR Michelin OR BYD OR GWM) fábrica Brasil investimento', "hint": "automotivo_brands"},
    {"q": '("data center" OR "centro de dados") (constrói OR investe OR anuncia) Brasil (bilhões OR milhões)', "hint": "tech"},
    {"q": '("nova fábrica" OR "amplia") (alimentos OR bebidas OR cervejaria OR frigorífico OR laticínio) Brasil investimento', "hint": "alimentos"},
    {"q": '("nova planta" OR "complexo industrial") (química OR petroquímica OR fertilizante OR "hidrogênio verde") Brasil milhões', "hint": "quimica"},
    {"q": '("gigafactory" OR "fábrica de baterias" OR "fábrica de semicondutores" OR "planta de células") Brasil investimento', "hint": "bateria_semi"},
    {"q": '("atraiu investimento" OR "anuncia investimento" OR "vai investir") fábrica (governo OR estado OR governador) Brasil bilhões', "hint": "fomento"},
]

# FIX 1: mapeamento setor_inferido (Haiku) → setor_alvo canônico
SETOR_MAP = {
    "automotivo": "automotivo", "autopeças": "automotivo", "autopecas": "automotivo",
    "eletro": "eletro", "eletrodoméstico": "eletro", "eletrodomesticos": "eletro", "eletrônico": "eletro",
    "alimentos": "alimentos", "bebidas": "alimentos", "alimentos e bebidas": "alimentos",
    "química": "quimica", "quimica": "quimica", "petroquímica": "quimica", "petroquimica": "quimica",
    "tecnologia": "tech", "data center": "tech", "tech": "tech",
    "logistica": "logistica", "logística": "logistica",
    "logistico": "logistica", "logístico": "logistica",
    "armazenagem": "logistica", "armazenamento": "logistica",
}


def mapear_setor_alvo(setor_inferido: str | None) -> str | None:
    """FIX 1+2: retorna setor_alvo canônico ou None pra rejeitar (Outro/fora-lacuna)."""
    if not setor_inferido:
        return None
    s = setor_inferido.strip().lower()
    return SETOR_MAP.get(s)


# Estagio 2: mapeia setor_alvo (lowercase short) -> setor canonico de obras (UPPERCASE_LONG)
SETOR_CANONICO_OBRAS = {
    "automotivo": "AUTOMOTIVO_E_AUTOPECAS",
    "alimentos": "ALIMENTOS_E_BEBIDAS",
    "quimica": "QUIMICA",
    "tech": "TECNOLOGIA",
    "eletro": "ELETRODOMESTICOS",
    "logistica": "LOGISTICO",
    "logística": "LOGISTICO",
    "logístico": "LOGISTICO",
    "armazenagem": "LOGISTICO",
}

FASES_VALIDAS_ESTAGIO2 = ("PLANEJAMENTO", "EM_EXECUCAO", "LICENCA_INSTALACAO")

HAIKU_PROMPT = """Você extrai metadados de notícias de obras físicas reais no Brasil (industrial ou de infraestrutura, públicas ou privadas).

Analise título+snippet e retorne JSON (sem ```fences) OU a string null.

REJEITAR (retornar null):
- Não é anúncio de obra/fábrica/planta/armazém/instalação física no Brasil
- Opinião/análise/coluna sem investimento concreto
- Notícia internacional sem operação Brasil
- Lançamento de produto/modelo (não fábrica)
- Resultado financeiro/balanço/M&A sem obra física

ACEITAR (retornar JSON):
{
  "empresa": "Razão social ou marca anunciada (privada, estatal ou governamental)",
  "setor": "Automotivo|Alimentos|Quimica|Tecnologia|Eletro|Logistica|Outro",
  "capex_mi": 250,
  "uf": "PR",
  "capex_suspeito": false
}

Regras:
- capex_mi: em milhões R$. Se não mencionado, null. Se "bilhões", multiplicar (R$1bi = 1000).
- uf: sigla 2 letras se mencionada cidade/estado, senão null.
- setor: classificar conforme as 6 lacunas; Logistica cobre armazenagem/centro de distribuição/silos/portos secos/terminais; Outro se não bater.
- capex_suspeito: true se valor parecer investimento GLOBAL, programa plurianual, ou cobrir múltiplas
  plantas/países (ex: R$11bi Toyota Brasil 2030, R$37bi Petrobras SP 2026-30). Extraia só capex da
  OBRA ESPECÍFICA; se vier valor inflado, marque capex_mi=null e capex_suspeito=true.

Notícia:
"""


def hash_link(link: str) -> str:
    return hashlib.md5(link.encode("utf-8", "ignore")).hexdigest()[:16]


def chamar_serper(query: str) -> list[dict]:
    headers = {"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"}
    body = {"q": query, "gl": "br", "hl": "pt", "num": RESULTS_PER_QUERY, "tbs": "qdr:d"}
    r = requests.post(SERPER_URL, headers=headers, json=body, timeout=SERPER_TIMEOUT)
    r.raise_for_status()
    return r.json().get("news", []) or []


def chamar_haiku(client, titulo: str, snippet: str) -> dict | None:
    payload = HAIKU_PROMPT + f"\nTítulo: {titulo}\nSnippet: {snippet[:800]}"
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": payload}],
    )
    texto = resp.content[0].text.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)
        texto = texto.strip()
    if texto.lower() == "null" or not texto.startswith("{"):
        return None
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        log.warning("Haiku JSON inválido: %s", texto[:150])
        return None


def processar_query(query_cfg: dict, conn, client, dry: bool) -> dict:
    q = query_cfg["q"]
    hint = query_cfg["hint"]
    log.info("Query [hint=%s]: %s", hint, q)
    try:
        items = chamar_serper(q)
    except Exception as e:
        log.exception("Serper falhou: %s", e)
        return {"encontrados": 0, "novos": 0, "pulados": 0, "rejeitados": 0}

    log.info("  %d resultados Serper", len(items))
    encontrados = len(items)
    novos = pulados = rejeitados = fora_lacuna = dedup_obra = flag_capex = 0
    cur = conn.cursor()

    for item in items:
        titulo = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not titulo or not link:
            continue

        lh = hash_link(link)
        cur.execute("SELECT 1 FROM candidatos_industrial WHERE link_hash=%s", (lh,))
        if cur.fetchone():
            pulados += 1
            continue

        dados = chamar_haiku(client, titulo, snippet or titulo)
        if not dados:
            rejeitados += 1
            log.info("  REJEITADO Haiku: %s", titulo[:80])
            continue

        empresa = (dados.get("empresa") or "").strip() or None
        setor_inferido = (dados.get("setor") or "").strip() or None
        capex = dados.get("capex_mi")
        try:
            capex = float(capex) if capex is not None else None
        except (TypeError, ValueError):
            capex = None
        uf = (dados.get("uf") or "").strip().upper() or None
        if uf and len(uf) != 2:
            uf = None
        capex_suspeito = bool(dados.get("capex_suspeito"))

        # FIX 1+2: mapear setor_alvo do setor_inferido; rejeitar fora-lacuna (Outro)
        setor_alvo = mapear_setor_alvo(setor_inferido)
        if not setor_alvo:
            fora_lacuna += 1
            log.info("  FORA_LACUNA empresa=%s setor=%s | %s", empresa, setor_inferido, titulo[:60])
            continue

        # FIX 5: se Haiku marcou capex_suspeito, gravar com flag + capex=NULL
        flags_list = []
        if capex_suspeito:
            capex = None
            flags_list.append("capex_suspeito")
            flag_capex += 1

        # FIX 3: cross-dedup vs obras (empresa+uf, motivo_invisivel IS NULL)
        status = "novo"
        if empresa and uf:
            cur.execute(
                """SELECT id FROM obras
                   WHERE immutable_unaccent_lower(empresa) ILIKE '%%'||%s||'%%'
                     AND uf=%s AND motivo_invisivel IS NULL
                   LIMIT 1""",
                (empresa.lower(), uf),
            )
            row = cur.fetchone()
            if row:
                status = "dedup_obra"
                flags_list.append(f"dedup_match_obra:{row[0]}")
                dedup_obra += 1

        flags = ";".join(flags_list) if flags_list else None
        log.info(
            "  %s empresa=%s setor_alvo=%s capex_mi=%s uf=%s flags=%s | %s",
            "DEDUP" if status == "dedup_obra" else "ACEITO",
            empresa, setor_alvo, capex, uf, flags, titulo[:60],
        )
        if dry:
            novos += 1
            continue

        cur.execute(
            """
            INSERT INTO candidatos_industrial
                (titulo, snippet, url, link_hash, empresa_extraida, setor_inferido,
                 setor_alvo, capex_mencionado_mi, uf_inferida, query_origem, status, flags)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (link_hash) DO NOTHING
            """,
            (titulo, snippet, link, lh, empresa, setor_inferido, setor_alvo, capex, uf, q, status, flags),
        )
        if cur.rowcount:
            novos += 1
        else:
            pulados += 1

    conn.commit()
    return {
        "encontrados": encontrados, "novos": novos, "pulados": pulados, "rejeitados": rejeitados,
        "fora_lacuna": fora_lacuna, "dedup_obra": dedup_obra, "flag_capex": flag_capex,
    }


# ============================================================================
# ESTAGIO 2 (--processar)
# ============================================================================

def _chamar_haiku_raw(client, prompt: str, max_tokens: int = 400) -> str | None:
    """Helper: chama Haiku e retorna texto cru (sem ```fences)."""
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = resp.content[0].text.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)
        texto = texto.strip()
    return texto or None


def validar_obra_real(client, candidato: dict) -> dict:
    """Estagio 2: Haiku valida se candidato e obra fisica real (rejeita M&A/anuncio/opiniao)."""
    empresa = candidato.get("empresa_extraida") or "?"
    setor = candidato.get("setor_alvo") or "?"
    capex = candidato.get("capex_mencionado_mi")
    uf = candidato.get("uf_inferida") or "?"
    titulo = candidato.get("titulo") or ""
    url = candidato.get("url") or ""
    snippet = (candidato.get("snippet") or "")[:500]
    prompt = (
        "Voce valida se um candidato e OBRA FISICA REAL (construcao/ampliacao/instalacao industrial ou de infraestrutura).\n\n"
        f"Empresa: {empresa}\n"
        f"Setor: {setor}\n"
        f"Capex: R$ {capex if capex is not None else 'n/d'} mi\n"
        f"UF: {uf}\n"
        f"Titulo: {titulo}\n"
        f"Snippet: {snippet}\n"
        f"URL: {url}\n\n"
        "REJEITAR (obra_real=false) se:\n"
        "- M&A / aquisicao / fusao sem obra fisica\n"
        "- Lancamento de produto/modelo (nao instalacao)\n"
        "- Opiniao / coluna / analise sem investimento concreto\n"
        "- Resultado financeiro / balanco\n"
        "- Anuncio internacional sem operacao Brasil\n\n"
        "ACEITAR (obra_real=true) se:\n"
        "- Construcao / ampliacao / instalacao confirmada (industrial, logistica, infraestrutura)\n"
        "- Anuncio concreto com empresa + localizacao + capex (ou intencao explicita)\n"
        "- Empresa pode ser privada, estatal ou governamental (ex: Conab, Petrobras, EPE) -- o gate e obra fisica real, nao natureza juridica\n\n"
        'Responda APENAS JSON (sem fences): {"obra_real": true|false, "motivo": "...", "fase_sugerida": "PLANEJAMENTO|EM_EXECUCAO|LICENCA_INSTALACAO"}'
    )
    texto = None
    try:
        texto = _chamar_haiku_raw(client, prompt, max_tokens=400)
        if not texto:
            return {"obra_real": False, "motivo": "haiku_empty"}
        return json.loads(texto)
    except json.JSONDecodeError:
        log.warning("Haiku validacao JSON invalido: %s", (texto or "")[:150])
        return {"obra_real": False, "motivo": "parse_error"}
    except Exception as e:
        log.exception("Haiku validacao falhou: %s", e)
        return {"obra_real": False, "motivo": f"erro:{type(e).__name__}"}


def _brasilapi_cnpj(cnpj: str) -> dict | None:
    """Lookup CNPJ no BrasilAPI. Retorna dict ou None. (Forward-compatible; Estagio 1 nao guarda CNPJ.)"""
    cnpj_clean = re.sub(r"\D", "", cnpj or "")
    if len(cnpj_clean) != 14:
        return None
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_clean}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def processar_candidato(conn, client, candidato: dict, dry_run: bool = False) -> dict:
    """Estagio 2: valida + insere obra a partir de um candidato_industrial."""
    cid = candidato["id"]
    empresa = (candidato.get("empresa_extraida") or "").strip()
    setor_alvo = candidato.get("setor_alvo")
    uf = (candidato.get("uf_inferida") or "").strip().upper() or None
    capex_mi = candidato.get("capex_mencionado_mi")
    if capex_mi is not None:
        try:
            capex_mi = float(capex_mi)
        except (TypeError, ValueError):
            capex_mi = None

    # Step 1: BrasilAPI CNPJ - candidatos_industrial nao guarda CNPJ ainda (Estagio 1 TODO).
    # Forward-compatible: pega de 'cnpj_hint' se algum dia existir.
    cnpj_validado = None
    uf_confirmada = uf
    cnpj_hint = candidato.get("cnpj_hint")
    if cnpj_hint:
        info = _brasilapi_cnpj(cnpj_hint)
        if info:
            cnpj_validado = re.sub(r"\D", "", cnpj_hint)
            uf_confirmada = info.get("uf") or uf

    # Step 2: re-confirmar dedup vs obras (race-safe vs Estagio 1)
    if empresa and uf_confirmada:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM obras
                   WHERE immutable_unaccent_lower(empresa) ILIKE '%%'||%s||'%%'
                     AND uf=%s AND motivo_invisivel IS NULL
                   LIMIT 1""",
                (empresa.lower(), uf_confirmada),
            )
            dup = cur.fetchone()
        if dup:
            motivo = f"dedup_obra:{dup[0]}"
            log.info("  DEDUP candidato=%s -> obra=%s | %s/%s", cid, dup[0], empresa, uf_confirmada)
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE candidatos_industrial SET status='rejeitado', motivo_rejeicao=%s WHERE id=%s",
                        (motivo[:200], cid),
                    )
                conn.commit()
            return {"id": str(cid), "status": "rejeitado", "motivo": motivo}

    # Step 3: validacao Haiku (obra real?)
    validacao = validar_obra_real(client, candidato)
    if not validacao.get("obra_real"):
        motivo = (validacao.get("motivo") or "haiku_rejeitou")[:200]
        log.info("  REJEITADO candidato=%s motivo=%s | %s", cid, motivo, empresa)
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE candidatos_industrial SET status='rejeitado', motivo_rejeicao=%s WHERE id=%s",
                    (motivo, cid),
                )
            conn.commit()
        return {"id": str(cid), "status": "rejeitado", "motivo": motivo}

    # Step 4: monta payload da obra
    fase = validacao.get("fase_sugerida") or "PLANEJAMENTO"
    if fase not in FASES_VALIDAS_ESTAGIO2:
        fase = "PLANEJAMENTO"
    valor_estimado = int(capex_mi * 1_000_000) if capex_mi else None
    setor_obras = SETOR_CANONICO_OBRAS.get(setor_alvo) if setor_alvo else None
    nome = (candidato.get("titulo") or empresa or "Obra industrial")[:300]
    obs = f"Fonte: {candidato.get('url','')} | Validado Estagio 2 (Haiku) | candidato_id={cid}"

    if dry_run:
        log.info(
            "  DRY_APROVADO empresa=%s setor=%s fase=%s capex_mi=%s uf=%s",
            empresa, setor_obras, fase, capex_mi, uf_confirmada,
        )
        return {
            "id": str(cid), "status": "dry_run_aprovado", "empresa": empresa,
            "setor": setor_obras, "fase": fase, "capex_mi": capex_mi, "uf": uf_confirmada,
            "motivo": validacao.get("motivo"),
        }

    # Step 5: INSERT obras + recompute + UPDATE candidato
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO obras (empresa, nome, setor, uf, fase, valor_estimado,
                fonte, fonte_tipo, validacao_obra_at, observacoes_validacao, cnpj, url_fonte)
            VALUES (%s, %s, %s, %s, %s, %s,
                'industrial_priv_auto', 'NOTICIA', NOW(), %s, %s, %s)
            RETURNING id
            """,
            (empresa or None, nome, setor_obras, uf_confirmada, fase, valor_estimado,
             obs, cnpj_validado, candidato.get("url") or None),
        )
        obra_id = cur.fetchone()[0]
        cur.execute("SELECT recompute_classificacao_obra(%s)", (obra_id,))
        cur.execute(
            "UPDATE candidatos_industrial SET status='cadastrado', obra_id=%s WHERE id=%s",
            (obra_id, cid),
        )
    conn.commit()
    log.info("  CADASTRADO obra=%s candidato=%s | %s/%s/%s", obra_id, cid, empresa, setor_obras, uf_confirmada)
    return {"id": str(cid), "status": "cadastrado", "obra_id": str(obra_id), "empresa": empresa}


def processar_lote(conn, client, dry_run: bool = False, limite: int = 20) -> dict:
    """Estagio 2: SELECT candidatos elegiveis + processa."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, titulo, snippet, url, empresa_extraida, setor_alvo,
                   uf_inferida, capex_mencionado_mi, flags
            FROM candidatos_industrial
            WHERE status = 'novo'
              AND setor_alvo IS NOT NULL
              AND (flags IS NULL OR flags NOT LIKE %s)
            ORDER BY criado_em ASC
            LIMIT %s
            """,
            ("%capex_suspeito%", limite),
        )
        cols = [d[0] for d in cur.description]
        candidatos = [dict(zip(cols, row)) for row in cur.fetchall()]

    log.info("Estagio 2: %d candidatos elegiveis (dry_run=%s, limite=%s)",
             len(candidatos), dry_run, limite)
    resultados = []
    for c in candidatos:
        r = processar_candidato(conn, client, c, dry_run=dry_run)
        resultados.append(r)
        time.sleep(0.5)  # rate limit Haiku

    cadastrados = sum(1 for r in resultados if r["status"] == "cadastrado")
    rejeitados = sum(1 for r in resultados if r["status"] == "rejeitado")
    dry_aprovados = sum(1 for r in resultados if r["status"] == "dry_run_aprovado")
    stats = {
        "estagio": 2, "processados": len(resultados),
        "cadastrados": cadastrados, "rejeitados": rejeitados,
        "dry_run_aprovados": dry_aprovados, "dry_run": dry_run,
    }
    log.info("=== FIM Estagio 2 === %s", stats)
    print(f"STATS_JSON: {json.dumps({'estagio2': stats, 'detalhes': resultados}, default=str)}")
    return stats


# ============================================================================
# MAIN
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--descoberta", action="store_true",
                    help="Estagio 1: enfileira candidatos via Serper+Haiku")
    ap.add_argument("--processar", action="store_true",
                    help="Estagio 2: valida candidatos_industrial -> cadastra obras")
    ap.add_argument("--limite", type=int, default=20,
                    help="Estagio 2: max candidatos por run (default 20)")
    ap.add_argument("--dry-run", action="store_true", help="Nao persiste, so loga")
    args = ap.parse_args()

    if not (args.descoberta or args.processar):
        log.error("--descoberta ou --processar obrigatorio")
        sys.exit(2)
    if args.descoberta and args.processar:
        log.error("Use --descoberta OU --processar, nao ambos")
        sys.exit(2)

    if "ANTHROPIC_API_KEY" not in os.environ:
        log.error("ANTHROPIC_API_KEY ausente")
        sys.exit(2)
    if args.descoberta and "SERPER_API_KEY" not in os.environ:
        log.error("SERPER_API_KEY ausente (necessario para --descoberta)")
        sys.exit(2)

    client = anthropic.Anthropic()
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    if args.descoberta:
        totais = {"encontrados": 0, "novos": 0, "pulados": 0, "rejeitados": 0,
                  "fora_lacuna": 0, "dedup_obra": 0, "flag_capex": 0}
        por_query = []
        for query_cfg in QUERIES:
            stats = processar_query(query_cfg, conn, client, args.dry_run)
            stats["hint"] = query_cfg["hint"]
            por_query.append(stats)
            for k, v in stats.items():
                if k in totais:
                    totais[k] += v
        log.info("=== FIM Estagio 1 === %s", totais)
        print(f"STATS_JSON: {json.dumps({'estagio1': totais, 'por_query': por_query})}")
    else:  # args.processar
        processar_lote(conn, client, dry_run=args.dry_run, limite=args.limite)

    conn.close()


if __name__ == "__main__":
    main()
