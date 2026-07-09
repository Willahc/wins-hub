"""
Orchestrator — execução noturna unificada.

Sequencia os 6 captadores existentes e dispara matchmaking apenas para obras
inseridas ou atualizadas dentro da janela. Janela de matchmaking: 02:30–07:00 BRT
(o teto é 07:00 — depois disso, matchmaking é pulado).

Roda dentro do container API:
    docker exec wins_hub-api-1 python /app/scripts/orchestrator.py [--dry-run]

Cada execução grava linhas em log_captacao com:
    fonte='ORCHESTRATOR'  → run principal (status final + duração total)
    fonte='captar_xxx'    → 1 linha por captador
    fonte='MATCHMAKING'   → status do matchmaking (sucesso | pulado | erro)
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2

# ── Config ────────────────────────────────────────────────────────────────────
BRT = ZoneInfo("America/Sao_Paulo")
JANELA_FIM_HORA = 7  # 07:00 BRT — após isso, NÃO inicia matchmaking
# 11/06: era 3600s (1h). O captador saudável mais lento é ~440s; 1h só servia
# pra deixar um captador travado comer 1h e estourar a janela. Baixado p/ 15min.
CAPTADOR_TIMEOUT_S = 900  # 15min por captador (2x o pior saudável)
# 11/06: teto do bloco de captadores. Impede que vários captadores lentos/travados
# em série empurrem o matchmaking pra fora da janela 07:00 BRT (run 06-10 durou 5h).
# Healthy run ~30-40min, então 90min é folgado; ao estourar, pula o resto e segue
# pro matchmaking (melhor matchmaking + maioria das fontes do que todas-as-fontes
# sem matchmaking).
CAPTADORES_DEADLINE_S = 90 * 60
MAX_OBRAS_MATCHMAKING = 1000  # cap por execução (defesa contra backfill espúrio)

# Ordem de execução: mais leves/críticos primeiro (ibama é diário e curto)
CAPTADORES: list[tuple[str, str, list[str]]] = [
    # OFICIAL (estruturado: CSV/JSON/XLSX)
    ("captar_ibama", "/app/scripts/captar_ibama.py", []),
    ("captar_bndes", "/app/scripts/captar_bndes.py", []),
    ("captar_aneel", "/app/scripts/captar_aneel.py", []),
    ("captar_antaq", "/app/scripts/captar_antaq.py", []),
    # ("captar_anm", "/app/scripts/captar_anm.py", []),  # DESATIVADO 16/06: CFEM=royalty/producao, nao obra (100% ruido)
    ("captar_cvm",   "/app/scripts/captar_cvm.py", []),
    ("captar_suframa_cas_industrial", "/app/scripts/captar_suframa_cas_industrial.py", []),
    # NOTICIA (RSS/WP API — fonte_tipo='NOTICIA', excluído de is_ouro até validação)
    # APOSENTADOS Fase 2 portão (18/06): dumpers crus sem Haiku, ~6% yield, despejavam NULL.
    # Notícia agora entra só via captar_noticias_setoriais (Haiku+portão). Ver app/scripts/portao.
    # ("captar_cimm",          "/app/scripts/captar_cimm.py", []),
    # ("captar_agenciainfra",  "/app/scripts/captar_agenciainfra.py", []),
    # NOTICIA + LLM (RSS multi-fonte + Haiku extração estruturada)
    ("captar_noticias_setoriais", "/app/scripts/captar_noticias_setoriais.py", []),
    # PNCP (Portal Nacional de Contratações Públicas — sprint mapeamento dia 1)
    ("captar_pncp_obras",    "/app/scripts/captar_pncp_obras.py", []),
    # Civil 100k: amplia cobertura para obras civis pequenas/medias
    # (R$ 100 mil+) sem alterar o piso industrial dos captadores PNCP existentes.
    ("captar_pncp_civil_100k", "/app/scripts/captar_pncp_civil_100k.py", []),
    # ObrasGov: projetos de investimento em infraestrutura com recurso publico.
    ("captar_obrasgov_100k", "/app/scripts/captar_obrasgov_100k.py", []),
    # Recife: licenciamento urbanistico municipal, sem API key.
    ("captar_recife_licenciamento_100k", "/app/scripts/captar_recife_licenciamento_100k.py", []),
    # Curitiba: base de alvaras municipal atualizada mensalmente.
    ("captar_curitiba_alvaras_100k", "/app/scripts/captar_curitiba_alvaras_100k.py", []),
    # GeoSampa: alvaras HIS/HMP com area construida e deferimento.
    ("captar_geosampa_habitacao_popular_100k", "/app/scripts/captar_geosampa_habitacao_popular_100k.py", []),
    ("captar_pncp_consulta", "/app/scripts/captar_pncp_consulta.py", []),
    ("captar_pncp_defesa",   "/app/scripts/captar_pncp_defesa.py", []),
    # Sprint dia 2: DOU (InLabs), Eletrobras/Axia RI (Playwright), ANP (scaffold)
    ("captar_dou_inlabs",    "/app/scripts/captar_dou_inlabs.py", []),
    ("captar_eletrobras_ri", "/app/scripts/captar_eletrobras_ri.py", []),
    ("captar_anp",           "/app/scripts/captar_anp.py", []),
    # Sprint dia 4 (Sessão 1): DOEs estaduais piloto (RJ/MG/RS/PR)
    # Framework multi-backend instalado; volume real depende do estado.
    ("captar_doe_rj", "/app/scripts/captar_doe.py", ["--uf", "rj"]),
    ("captar_doe_mg", "/app/scripts/captar_doe.py", ["--uf", "mg"]),
    ("captar_doe_rs", "/app/scripts/captar_doe.py", ["--uf", "rs"]),
    ("captar_doe_pr", "/app/scripts/captar_doe.py", ["--uf", "pr"]),
    # 03/06 Tier A: PA (Vale S11D/MRN/Alunorte), MS (frigorificos/celulose),
    # GO (Aclara/mineracao). Yield depende de fix de chunking PDF (Haiku 5k chars).
    ("captar_doe_pa", "/app/scripts/captar_doe.py", ["--uf", "pa"]),
    ("captar_doe_ms", "/app/scripts/captar_doe.py", ["--uf", "ms"]),
    ("captar_doe_go", "/app/scripts/captar_doe.py", ["--uf", "go"]),
    # Sprint dia 5 (Sessão 2): DNIT (scaffold via gov.br/dnit) + DOE-SP (scaffold, GCP pendente)
    ("captar_dnit",   "/app/scripts/captar_dnit.py", []),
    ("captar_doe_sp", "/app/scripts/captar_doe_sp.py", []),
    # 16/05: Google Alerts RSS (7 feeds, requer GOOGLE_ALERTS_FEEDS env ou FEEDS_DEFAULT no script)
    ("captar_google_alerts", "/app/scripts/captar_google_alerts.py", []),
    # 16/05 sessão noite: PNCP completo (30d, R$10mi+) + scaffolds estaduais SP / ANTT v2 / saneamento PAC / transparência
    ("captar_pncp_full",     "/app/scripts/captar_pncp_full.py", []),
    ("captar_der_sp",        "/app/scripts/captar_der_sp.py", []),
    ("captar_cdhu_sp",       "/app/scripts/captar_cdhu_sp.py", []),
    ("captar_sabesp_sp",     "/app/scripts/captar_sabesp_sp.py", []),
    ("captar_antt_rod_v2",   "/app/scripts/captar_antt_rod_v2.py", []),
    ("captar_saneamento",    "/app/scripts/captar_saneamento.py", []),
    ("captar_transparencia", "/app/scripts/captar_transparencia.py", []),
    # 16/05 noite v3: BNDES rodada extra c/ filtro saneamento (id_externo distinto coexiste)
    ("captar_bndes_saneamento", "/app/scripts/captar_bndes.py", ["--saneamento"]),
    # 03/06: 3 captadores OFICIAL prioritarios (auditoria 78 fontes — gap 35 dias)
    ("captar_antt_ferro_pic",    "/app/scripts/captar_antt_ferro_pic.py", []),
    ("captar_aneel_transmissao", "/app/scripts/captar_aneel_transmissao.py", []),
    ("captar_debentures_infra",  "/app/scripts/captar_debentures_infra.py", []),
]

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("orchestrator")


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def now_brt() -> datetime:
    return datetime.now(BRT)


def janela_matchmaking_aberta() -> tuple[bool, str]:
    """Retorna (ok, motivo) onde motivo só é populado quando ok=False."""
    h = now_brt()
    if h.hour < JANELA_FIM_HORA:
        return True, ""
    return False, f"hora atual {h.strftime('%H:%M')} BRT >= {JANELA_FIM_HORA:02d}:00 (janela fechada)"


def log_captacao(fonte: str, status: str, novos: int = 0, buscados: int = 0,
                 erro: str | None = None, duracao_ms: int = 0, *, dry_run: bool = False) -> None:
    """Insere uma linha em log_captacao. No-op em dry-run."""
    if dry_run:
        log.info(f"[DRY] log_captacao: fonte={fonte} status={status} novos={novos} dur={duracao_ms}ms")
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO log_captacao (fonte, status, novos, buscados, erro, duracao_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (fonte, status, novos, buscados, erro, duracao_ms),
            )
        conn.commit()
    finally:
        conn.close()



def _parse_stats_json(stdout: str) -> dict | None:
    """Extrai a última linha 'STATS_JSON: {...}' do stdout de um captador.
    Retorna dict com keys buscados/novos/erros, ou None se ausente/inválido."""
    if not stdout:
        return None
    import json as _json
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("STATS_JSON:"):
            try:
                payload = _json.loads(line[len("STATS_JSON:"):].strip())
                return {
                    "buscados": int(payload.get("buscados", 0) or 0),
                    "novos": int(payload.get("novos", 0) or 0),
                    "erros": int(payload.get("erros", 0) or 0),
                }
            except Exception:
                return None
    return None


# ── Captadores ───────────────────────────────────────────────────────────────
def rodar_captador(name: str, path: str, *, dry_run: bool, args: list[str] | None = None) -> bool:
    args = args or []
    log.info(f"▶ {name} iniciando…")
    if dry_run:
        log.info(f"  [DRY] subprocess.run([python, {path}, {args}])")
        log_captacao(name, "pulado", erro="dry-run", dry_run=True)
        return True

    t0 = time.time()
    try:
        r = subprocess.run(
            ["python", path, *args],
            check=False, capture_output=True, text=True, timeout=CAPTADOR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        dur_ms = CAPTADOR_TIMEOUT_S * 1000
        log.error(f"  ✗ {name} TIMEOUT após {CAPTADOR_TIMEOUT_S}s")
        log_captacao(name, "erro", erro=f"timeout >{CAPTADOR_TIMEOUT_S}s", duracao_ms=dur_ms)
        return False
    except Exception as e:
        dur_ms = int((time.time() - t0) * 1000)
        log.exception(f"  ✗ {name} erro inesperado")
        log_captacao(name, "erro", erro=str(e)[:500], duracao_ms=dur_ms)
        return False

    dur_ms = int((time.time() - t0) * 1000)
    stats = _parse_stats_json(r.stdout or "") or {}
    buscados = stats.get("buscados", 0)
    novos = stats.get("novos", 0)
    erros_internos = stats.get("erros", 0)
    if r.returncode == 0:
        log.info(f"  ✓ {name} sucesso em {dur_ms}ms (buscados={buscados} novos={novos} erros={erros_internos})")
        log_captacao(name, "sucesso",
                     novos=novos, buscados=buscados,
                     erro=(f"{erros_internos} itens com erro interno" if erros_internos else None),
                     duracao_ms=dur_ms)
        return True
    erro_tail = ((r.stderr or "") + (r.stdout or ""))[-500:]
    log.error(f"  ✗ {name} exit={r.returncode}: {erro_tail[:200]}")
    log_captacao(name, "erro",
                 novos=novos, buscados=buscados,
                 erro=f"exit={r.returncode} | {erro_tail}",
                 duracao_ms=dur_ms)
    return False


# ── Populador de descrição sintética ──────────────────────────────────────────
def rodar_populador_sintetico(*, dry_run: bool) -> None:
    """
    Re-popula descricao_sintetica em obras com LENGTH(descricao) < 200.
    Sujeito à mesma janela horária do matchmaking — pula se hora_BRT >= 07:00.

    Por que rodar AQUI (após matchmaking): captadores podem ter sobrescrito
    descrições sintéticas anteriores com texto curto, derrubando obras-prata.
    Esta passagem regenera o sintético determinístico nas que ainda estão curtas.
    """
    log.info("▶ Verificando janela de descrição sintética…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ DESCRICAO_SINTETICA PULADO: {motivo}")
        log_captacao("DESCRICAO_SINTETICA", "pulado", erro=motivo, dry_run=dry_run)
        return

    if dry_run:
        log.info("  [DRY] janela aberta; pulando execução real")
        log_captacao("DESCRICAO_SINTETICA", "pulado", erro="dry-run", dry_run=True)
        return

    sys.path.insert(0, "/app")
    from scripts.sintetizador import gerar_descricao  # type: ignore

    from psycopg2.extras import RealDictCursor

    t0 = time.time()
    conn = get_conn()
    atualizadas = 0
    puladas_curtas = 0
    candidatas = 0
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id::text AS id, fonte, nome, empresa, cnpj, municipio, uf,
                       setor, valor_formatado, fase, descricao, url_fonte
                FROM obras
                WHERE LENGTH(COALESCE(descricao,'')) < 200
            """)
            obras = cur.fetchall()
        candidatas = len(obras)
        log.info(f"  obras com descrição < 200 chars: {candidatas}")
        if candidatas == 0:
            log_captacao("DESCRICAO_SINTETICA", "sucesso", novos=0, buscados=0, duracao_ms=0)
            return

        with conn.cursor() as cur:
            for o in obras:
                desc = gerar_descricao(dict(o))
                if len(desc) < 200:
                    # Defensivo: nome/empresa/url tão pobres que o template
                    # sai abaixo do threshold. Pula sem marcar como sintética.
                    puladas_curtas += 1
                    continue
                cur.execute(
                    "UPDATE obras SET descricao = %s, descricao_sintetica = true "
                    "WHERE id = %s",
                    (desc, o["id"]),
                )
                atualizadas += 1
        conn.commit()
    finally:
        conn.close()

    dur_ms = int((time.time() - t0) * 1000)
    erro_msg = f"{puladas_curtas} obras com template <200 chars" if puladas_curtas else None
    log.info(f"  ✓ DESCRICAO_SINTETICA: {atualizadas}/{candidatas} atualizadas, "
             f"{puladas_curtas} puladas, {dur_ms}ms")
    log_captacao("DESCRICAO_SINTETICA", "sucesso",
                 novos=atualizadas, buscados=candidatas,
                 erro=erro_msg, duracao_ms=dur_ms)


# ── Matchmaking ───────────────────────────────────────────────────────────────
def obras_modificadas_desde(snapshot_utc: datetime) -> tuple[set[str], set[str]]:
    """Retorna (novas, atualizadas) — sets de obra_id como strings."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text FROM obras WHERE criado_em >= %s",
                (snapshot_utc,),
            )
            novas = {r[0] for r in cur.fetchall()}

            cur.execute(
                "SELECT DISTINCT obra_id::text FROM obras_atualizacoes_log "
                "WHERE atualizado_em >= %s AND obra_id IS NOT NULL",
                (snapshot_utc,),
            )
            atualizadas = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    return novas, atualizadas


def rodar_matchmaking(snapshot_utc: datetime, *, dry_run: bool) -> None:
    log.info("▶ Verificando janela de matchmaking…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ matchmaking PULADO: {motivo}")
        log_captacao("MATCHMAKING", "pulado", erro=motivo, dry_run=dry_run)
        return

    if dry_run:
        log.info("  [DRY] janela aberta; pulando execução real")
        log_captacao("MATCHMAKING", "pulado", erro="dry-run", dry_run=True)
        return

    novas, atualizadas = obras_modificadas_desde(snapshot_utc)
    # Novas primeiro: garante que obras realmente novas sempre recebem matches
    # antes que cap/janela cortem o batch. "Atualizadas" inclui qualquer log em
    # obras_atualizacoes_log — incluindo first-time-non-null disparado pelo trigger
    # log_obras_changes em backfills (ex.: empresa NULL→'').
    obra_ids = sorted(novas) + sorted(atualizadas - novas)
    capeado = False
    if len(obra_ids) > MAX_OBRAS_MATCHMAKING:
        log.warning(f"  ⚠ batch de {len(obra_ids)} obras excede cap {MAX_OBRAS_MATCHMAKING}; "
                    f"processando primeiras {MAX_OBRAS_MATCHMAKING} (novas-first)")
        obra_ids = obra_ids[:MAX_OBRAS_MATCHMAKING]
        capeado = True

    log.info(f"  matchmaking para {len(obra_ids)} obras "
             f"({len(novas)} novas + {len(atualizadas)} atualizadas)"
             f"{' [CAPED]' if capeado else ''}")

    if not obra_ids:
        log_captacao("MATCHMAKING", "sucesso", novos=0, buscados=0, duracao_ms=0)
        return

    # Import só agora pra não pagar custo se janela estiver fechada
    sys.path.insert(0, "/app")
    from services.matchmaking import gerar_matches_para_obra  # type: ignore

    t0 = time.time()
    total_matches = 0
    erros = 0
    processadas = 0
    janela_fechou = False
    for obra_id in obra_ids:
        # Reverifica janela a cada obra: se passar das 07:00 BRT mid-loop, encerra
        # cedo em vez de segurar o lock por horas.
        ok, motivo = janela_matchmaking_aberta()
        if not ok:
            log.warning(f"  ⊘ matchmaking encerrado mid-loop após {processadas}/{len(obra_ids)}: {motivo}")
            janela_fechou = True
            break
        try:
            r = gerar_matches_para_obra(obra_id)
            total_matches += int(r.get("matches_gerados") or 0)
        except Exception as e:
            erros += 1
            log.exception(f"  matchmaking falhou para obra {obra_id}: {e}")
        processadas += 1

    dur_ms = int((time.time() - t0) * 1000)
    if janela_fechou or capeado:
        status = "parcial"
        partes = []
        if janela_fechou:
            partes.append(f"janela fechou em {processadas}/{len(obra_ids)}")
        if capeado:
            partes.append(f"capeado em {MAX_OBRAS_MATCHMAKING}")
        if erros:
            partes.append(f"{erros} obras com erro")
        erro_msg = "; ".join(partes)
    else:
        status = "sucesso" if erros == 0 else "erro"
        erro_msg = f"{erros} obras com erro" if erros else None
    log.info(f"  ✓ matchmaking finalizado: {total_matches} matches, "
             f"{processadas}/{len(obra_ids)} processadas, {erros} erros, {dur_ms}ms")
    log_captacao("MATCHMAKING", status,
                 novos=total_matches, buscados=len(obra_ids),
                 erro=erro_msg, duracao_ms=dur_ms)

    refresh_fornecedor_matches_summary()
    refresh_fornecedores_facetas_mv()


def refresh_fornecedores_facetas_mv() -> None:
    """Recalcula as 3 MVs que alimentam o fast-path de /api/fornecedores
    (load sem filtro). Roda depois de refresh_fornecedor_matches_summary
    porque score_bands_global e lista_global leem de fornecedor_matches_summary."""
    t0 = time.time()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_fornecedores_facetas_global;")
            cur.execute("REFRESH MATERIALIZED VIEW mv_fornecedores_score_bands_global;")
            cur.execute("REFRESH MATERIALIZED VIEW mv_fornecedores_lista_global;")
        conn.commit()
        log.info(f"  ✓ MVs facetas refreshed em {int((time.time()-t0)*1000)}ms")
    except Exception as e:
        conn.rollback()
        log.exception(f"  ✗ refresh MVs facetas falhou: {e}")
    finally:
        conn.close()


def refresh_fornecedor_matches_summary() -> None:
    """Re-popula fornecedor_matches_summary (~5MB, 53k rows) — feed da listagem
    /api/fornecedores. TRUNCATE+INSERT atomico em transacao; deixa stale somente
    se INSERT falhar (TRUNCATE rolled back)."""
    t0 = time.time()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE fornecedor_matches_summary;")
            cur.execute("""
                INSERT INTO fornecedor_matches_summary
                SELECT cnpj,
                       COUNT(*)::int AS qtd,
                       ROUND(AVG(score)::numeric, 0)::int AS score_medio
                FROM matches_obra_prestador
                GROUP BY cnpj;
            """)
            cur.execute("SELECT COUNT(*) FROM fornecedor_matches_summary;")
            n = cur.fetchone()[0]
            # P1: sync coluna denormalizada matches_count em fornecedores
            # (ORDER BY indexavel via idx_forn_uf_mc / idx_forn_cnae_mc / idx_forn_mc).
            cur.execute("""
                UPDATE fornecedores f SET matches_count = s.qtd
                FROM fornecedor_matches_summary s
                WHERE s.cnpj = f.cnpj AND f.matches_count <> s.qtd;
            """)
            cur.execute("""
                UPDATE fornecedores f SET matches_count = 0
                WHERE f.matches_count <> 0
                  AND NOT EXISTS (SELECT 1 FROM fornecedor_matches_summary s WHERE s.cnpj = f.cnpj);
            """)
            # P1: rebuild mapa cnpj->setor (feed do filtro de setor em /api/fornecedores)
            cur.execute("TRUNCATE fornecedor_setores;")
            cur.execute("""
                INSERT INTO fornecedor_setores
                SELECT DISTINCT m.cnpj, o.setor
                FROM matches_obra_prestador m JOIN obras o ON m.obra_id = o.id
                WHERE o.setor IS NOT NULL;
            """)
        conn.commit()
        log.info(f"  ✓ fornecedor_matches_summary refreshed: {n} rows, {int((time.time()-t0)*1000)}ms")
    except Exception as e:
        conn.rollback()
        log.exception(f"  ✗ refresh fornecedor_matches_summary falhou: {e}")
    finally:
        conn.close()


# ── Matchmaking V2 (matches_v2 — engine canônico do painel de obras) ─────────
MATCHMAKING_V2_TIMEOUT_CAP_S = 30 * 60  # 30min teto (incremental real ~2-3min)


def _finalizar_job_v2(job_id: str, status: str, erro: str | None = None) -> None:
    """Garante que o matchmaker_job não fique RODANDO órfão (bloquearia o admin)."""
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE matchmaker_jobs SET status=%s, finalizado_em=NOW(), erro=%s "
                    "WHERE id=%s AND finalizado_em IS NULL",
                    (status, erro, job_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"  _finalizar_job_v2 falhou: {e}")


def rodar_matchmaking_v2(*, dry_run: bool) -> None:
    """11/06: passo NOVO. Roda o matchmaker_worker V2 (matches_v2) em modo
    incremental — alimenta o painel de obras / bucket 'sem match' que o nightly
    antes NÃO tocava (só populava V1/matches_obra_prestador). Dentro da janela
    07:00 BRT, com timeout limitado ao tempo restante. O worker se auto-finaliza
    (CONCLUIDO/ERRO); só tratamos timeout pra não deixar job RODANDO órfão."""
    log.info("▶ Verificando janela de matchmaking V2 (matches_v2)…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ MATCHMAKING_V2 PULADO: {motivo}")
        log_captacao("MATCHMAKING_V2", "pulado", erro=motivo, dry_run=dry_run)
        return
    if dry_run:
        log.info("  [DRY] janela aberta; pulando execução real")
        log_captacao("MATCHMAKING_V2", "pulado", erro="dry-run", dry_run=True)
        return

    # Não roda concorrente com um job admin já em andamento.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM matchmaker_jobs WHERE status='RODANDO' LIMIT 1")
            if cur.fetchone():
                log.warning("  ⊘ MATCHMAKING_V2 PULADO: já há job RODANDO")
                log_captacao("MATCHMAKING_V2", "pulado", erro="job_rodando")
                return
            cur.execute(
                "INSERT INTO matchmaker_jobs (iniciado_por, status, modo) "
                "VALUES ('orchestrator_nightly','RODANDO','incremental') RETURNING id"
            )
            job_id = str(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    # timeout = tempo restante até 07:00 BRT, com teto de 30min
    h = now_brt()
    fim = h.replace(hour=JANELA_FIM_HORA, minute=0, second=0, microsecond=0)
    restante_s = max(60, int((fim - h).total_seconds()))
    timeout_s = min(restante_s, MATCHMAKING_V2_TIMEOUT_CAP_S)

    t0 = time.time()
    try:
        r = subprocess.run(
            ["python", "-u", "/app/scripts/matchmaker_worker.py", job_id, "incremental"],
            check=False, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        dur_ms = int((time.time() - t0) * 1000)
        _finalizar_job_v2(job_id, "ERRO", "timeout orchestrator")
        log.error(f"  ✗ MATCHMAKING_V2 TIMEOUT após {timeout_s}s")
        log_captacao("MATCHMAKING_V2", "erro", erro=f"timeout >{timeout_s}s", duracao_ms=dur_ms)
        return

    dur_ms = int((time.time() - t0) * 1000)
    obras_proc = matches = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(obras_processadas,0), COALESCE(matches_criados,0) "
                "FROM matchmaker_jobs WHERE id=%s", (job_id,)
            )
            row = cur.fetchone()
            if row:
                obras_proc, matches = int(row[0]), int(row[1])
    finally:
        conn.close()

    if r.returncode == 0:
        log.info(f"  ✓ MATCHMAKING_V2: {matches} matches em {obras_proc} obras ({dur_ms}ms)")
        log_captacao("MATCHMAKING_V2", "sucesso", novos=matches, buscados=obras_proc, duracao_ms=dur_ms)
    else:
        # worker pode ter saído !=0 sem finalizar o job — garante limpeza
        _finalizar_job_v2(job_id, "ERRO", f"exit={r.returncode}")
        tail = ((r.stderr or "") + (r.stdout or ""))[-300:]
        log.error(f"  ✗ MATCHMAKING_V2 exit={r.returncode}: {tail[:150]}")
        log_captacao("MATCHMAKING_V2", "erro", novos=matches, buscados=obras_proc,
                     erro=f"exit={r.returncode} | {tail}", duracao_ms=dur_ms)


# ── Regen matches V1 por prestador cadastrado ────────────────────────
def rodar_regenerar_matches_prestadores(*, dry_run: bool) -> None:
    """13/06: passo NOVO. Regenera matches V1 (matches_obra_prestador, ranking
    on-demand 999) para TODOS os prestadores cadastrados com CNPJ. O passo
    obra-driven (rodar_matchmaking) so grava o top-50 fornecedores por obra: um
    prestador real fica fora de toda obra em que nao ranqueia no top-50, e como
    `gerar_matches_para_prestador` antes so disparava no login, quem logou antes
    do trigger / nunca logou ficava com 0 match permanente (ex.: LOGGIA: 16.575
    obras compativeis, 0 entregues). Este sweep -- barato, ~dezenas de
    prestadores -- garante a cobertura da demanda toda noite."""
    log.info("▶ Regenerando matches V1 por prestador cadastrado…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ REGEN_MATCHES_PRESTADOR PULADO: {motivo}")
        log_captacao("REGEN_MATCHES_PRESTADOR", "pulado", erro=motivo, dry_run=dry_run)
        return
    if dry_run:
        log.info("  [DRY] janela aberta; pulando execucao real")
        log_captacao("REGEN_MATCHES_PRESTADOR", "pulado", erro="dry-run", dry_run=True)
        return

    sys.path.insert(0, "/app")
    from services.matchmaking import gerar_matches_para_prestador  # type: ignore

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM prestadores "
                "WHERE ativo AND excluido_em IS NULL AND cnpj IS NOT NULL AND cnpj <> ''"
            )
            pids = [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()

    t0 = time.time()
    total_matches = com_match = erros = 0
    for pid in pids:
        try:
            r = gerar_matches_para_prestador(pid)
            n = int(r.get("matches_gerados") or 0)
            total_matches += n
            if n:
                com_match += 1
        except Exception as e:
            erros += 1
            log.exception(f"  regen matches falhou para prestador {pid}: {e}")

    dur_ms = int((time.time() - t0) * 1000)
    status = "sucesso" if erros == 0 else "erro"
    log.info(f"  ✓ REGEN_MATCHES_PRESTADOR: {total_matches} matches em "
             f"{com_match}/{len(pids)} prestadores ({erros} erros, {dur_ms}ms)")
    log_captacao("REGEN_MATCHES_PRESTADOR", status,
                 novos=total_matches, buscados=len(pids),
                 erro=(f"{erros} prestadores com erro" if erros else None),
                 duracao_ms=dur_ms)


# ── Wire-in decisores_empresa_alvo (leads pré-cadastrados) ───────────────────
def rodar_wire_in_decisores_empresa_alvo(*, dry_run: bool) -> None:
    """Aplica leads de `decisores_empresa_alvo` (v3-validated) em obras OURO/PRATA
    sem nivel1_nome. Roda dentro da janela de matchmaking. BRONZE não entra
    aqui (feedback: enrichment massivo foca OURO+PRATA por default)."""
    log.info("▶ Verificando wire-in decisores_empresa_alvo…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ WIREIN_DECISORES_EMPRESA_ALVO PULADO: {motivo}")
        log_captacao("WIREIN_DECISORES_EMPRESA_ALVO", "pulado", erro=motivo, dry_run=dry_run)
        return
    if dry_run:
        log.info("  [DRY] janela aberta; pulando execução real")
        log_captacao("WIREIN_DECISORES_EMPRESA_ALVO", "pulado",
                     erro="dry-run", dry_run=True)
        return

    sys.path.insert(0, "/app")
    from scripts.apply_decisores_empresa_alvo import executar  # type: ignore

    t0 = time.time()
    try:
        stats = executar(tiers=("OURO", "PRATA"), dry_run=False, verbose=False)
    except Exception as e:
        dur_ms = int((time.time() - t0) * 1000)
        log.exception(f"  ✗ WIREIN_DECISORES_EMPRESA_ALVO erro: {e}")
        log_captacao("WIREIN_DECISORES_EMPRESA_ALVO", "erro",
                     erro=str(e)[:500], duracao_ms=dur_ms)
        return

    dur_ms = int((time.time() - t0) * 1000)
    log.info(f"  ✓ WIREIN_DECISORES_EMPRESA_ALVO: leads={stats['leads']} "
             f"candidatas={stats['candidatas']} matched={stats['matched']} "
             f"ambíguas={stats['ambiguos']} atualizadas={stats['atualizadas']} "
             f"({dur_ms}ms)")
    log_captacao("WIREIN_DECISORES_EMPRESA_ALVO", "sucesso",
                 novos=stats["atualizadas"], buscados=stats["candidatas"],
                 erro=(f"{stats['ambiguos']} ambíguas" if stats["ambiguos"] else None),
                 duracao_ms=dur_ms)


# ── Enrichment decisor top OURO (P3.1 ao vivo, sem Hunter) ───────────────────
ENRICHMENT_DECISOR_CAP_RUNTIME_S = 10 * 60  # cap defensivo 10min


def rodar_enrichment_decisor_top_ouro(*, dry_run: bool) -> None:
    """Top 5 obras OURO criadas hoje (lead_score desc) cujo CNPJ não tem decisor
    em cache há <=30 dias → roda P3.1 (dominio → search → email).
    Hunter desligado (permitir_hunter=False). Cap runtime 10min total."""
    log.info("▶ Verificando enrichment decisor top OURO…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ ENRICHMENT_DECISOR_TOP_OURO PULADO: {motivo}")
        log_captacao("ENRICHMENT_DECISOR_TOP_OURO", "pulado", erro=motivo, dry_run=dry_run)
        return
    if dry_run:
        log.info("  [DRY] janela aberta; pulando execução real")
        log_captacao("ENRICHMENT_DECISOR_TOP_OURO", "pulado", erro="dry-run", dry_run=True)
        return

    t0 = time.time()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.id::text, o.cnpj, o.empresa, o.nome, o.lead_score
                FROM obras o
                WHERE o.criado_em::date = CURRENT_DATE
                  AND o.cnpj IS NOT NULL AND LENGTH(o.cnpj) = 14
                  AND o.nivel1_nome IS NOT NULL AND o.nivel1_nome != ''
                  AND (COALESCE(o.nivel1_email,'') != '' OR COALESCE(o.nivel1_linkedin,'') != '')
                  AND cargo_decisor_keyword(o.nivel1_cargo)
                  AND COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA'
                  AND o.cnpj NOT IN (
                      SELECT cnpj FROM empresa_decisores_cache
                      WHERE atualizado_em > NOW() - INTERVAL '30 days'
                  )
                ORDER BY o.lead_score DESC NULLS LAST
                LIMIT 5
            """)
            top5 = cur.fetchall()
    finally:
        conn.close()

    if not top5:
        dur_ms = int((time.time() - t0) * 1000)
        log.info(f"  ✓ ENRICHMENT_DECISOR_TOP_OURO: nenhuma obra elegivel hoje ({dur_ms}ms)")
        log_captacao("ENRICHMENT_DECISOR_TOP_OURO", "sucesso", novos=0, buscados=0, duracao_ms=dur_ms)
        return

    sys.path.insert(0, "/app")
    try:
        from sales_intelligence.camada3_decisores.orquestrador import descobrir_decisores
        from sales_intelligence.integracao_c3_c4 import enriquecer_decisores_com_email
        from sales_intelligence.db.cache_decisores import gravar_decisor
        from routes.decisor_lookup import _buscar_dominio_inline
    except Exception as e:
        dur_ms = int((time.time() - t0) * 1000)
        log.exception(f"  ✗ ENRICHMENT_DECISOR_TOP_OURO import erro: {e}")
        log_captacao("ENRICHMENT_DECISOR_TOP_OURO", "erro", erro=str(e)[:500], duracao_ms=dur_ms)
        return

    total_novos = 0
    total_com_email = 0
    erros = 0
    abortado_por_cap = False

    for (oid, cnpj, empresa, nome, lead) in top5:
        elapsed = time.time() - t0
        if elapsed > ENRICHMENT_DECISOR_CAP_RUNTIME_S:
            log.warning(f"  ⊘ CAP runtime {ENRICHMENT_DECISOR_CAP_RUNTIME_S}s atingido após {elapsed:.0f}s — abortando")
            abortado_por_cap = True
            break
        try:
            conn_d = get_conn()
            try:
                dominio = _buscar_dominio_inline(conn_d, cnpj, empresa)
            finally:
                conn_d.close()
            decisores = descobrir_decisores(cnpj, empresa or "", force_refresh=False)
            if dominio and decisores:
                decisores = enriquecer_decisores_com_email(
                    cnpj=cnpj, dominio_oficial=dominio,
                    decisores=decisores, permitir_hunter=False,
                )
            persistidos = 0
            com_email = 0
            for d in (decisores or []):
                try:
                    if gravar_decisor(d):
                        persistidos += 1
                    if getattr(d, "email", None):
                        com_email += 1
                except Exception as e:
                    log.warning(f"    gravar_decisor erro: {e}")
            total_novos += persistidos
            total_com_email += com_email
            log.info(f"  ▸ {(empresa or '<?>')[:35]:35s} cnpj={cnpj} lead={lead}: "
                     f"{persistidos} decisores, {com_email} c/email, dominio={dominio or 'none'}")
        except Exception as e:
            erros += 1
            log.warning(f"  ✗ erro enriching cnpj={cnpj}: {e}")

    dur_ms = int((time.time() - t0) * 1000)
    status = "sucesso" if (erros == 0 and not abortado_por_cap) else "erro"
    msg_erro = None
    if abortado_por_cap:
        msg_erro = f"cap_runtime atingido apos {dur_ms}ms"
    elif erros:
        msg_erro = f"{erros} obras com erro"
    log.info(f"  ✓ ENRICHMENT_DECISOR_TOP_OURO: alvos={len(top5)} novos={total_novos} "
             f"c/email={total_com_email} erros={erros} {dur_ms}ms")
    log_captacao("ENRICHMENT_DECISOR_TOP_OURO", status,
                 novos=total_novos, buscados=len(top5),
                 erro=msg_erro, duracao_ms=dur_ms)


# ── Intel comercial (subdomínios → tags) ──────────────────────────────────────
def rodar_intel_obras_ouro(*, dry_run: bool) -> None:
    """Coleta subdomínios via hackertarget para empresas de obra-ouro com domínio
    cadastrado em fornecedor_meta. Re-coleta só se a última > 7 dias.
    Mesma janela do matchmaking (pula após 07:00 BRT)."""
    log.info("▶ Verificando janela de intel comercial…")
    ok, motivo = janela_matchmaking_aberta()
    if not ok:
        log.warning(f"  ⊘ INTEL_COMERCIAL PULADO: {motivo}")
        log_captacao("INTEL_COMERCIAL", "pulado", erro=motivo, dry_run=dry_run)
        return
    if dry_run:
        log.info("  [DRY] janela aberta; pulando execução real")
        log_captacao("INTEL_COMERCIAL", "pulado", erro="dry-run", dry_run=True)
        return

    sys.path.insert(0, "/app")
    from services.recon_intel import coletar_intel_obras_ouro  # type: ignore

    t0 = time.time()
    try:
        r = coletar_intel_obras_ouro(max_idade_dias=7)
    except Exception as e:
        dur_ms = int((time.time() - t0) * 1000)
        log.exception(f"  ✗ INTEL_COMERCIAL erro: {e}")
        log_captacao("INTEL_COMERCIAL", "erro", erro=str(e)[:500], duracao_ms=dur_ms)
        return

    dur_ms = int((time.time() - t0) * 1000)
    status = "sucesso" if r.get("erros", 0) == 0 else "erro"
    log.info(f"  ✓ INTEL_COMERCIAL: alvos={r['alvos']} rodadas={r['rodadas']} "
             f"skips_recentes={r['skips_recentes']} erros={r['erros']} {dur_ms}ms")
    log_captacao("INTEL_COMERCIAL", status,
                 novos=r["rodadas"], buscados=r["alvos"],
                 erro=(f"{r['erros']} erros" if r["erros"] else None),
                 duracao_ms=dur_ms)


# ── Main ──────────────────────────────────────────────────────────────────────
_APP_LOCK_PATH = "/tmp/orchestrator_app.lock"


def _adquirir_lock_aplicacao():
    """Lock de processo dentro do container, complementa o lock do wrapper host."""
    fd = open(_APP_LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.warning("SKIP: outra instância do orchestrator detém o lock — abortando.")
        sys.exit(0)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def main() -> int:
    parser = argparse.ArgumentParser(description="Orchestrator de captação + matchmaking.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula sem rodar captadores nem gravar em log_captacao.")
    args = parser.parse_args()
    dry = args.dry_run

    _lock_fd = _adquirir_lock_aplicacao()

    log.info(f"=== ORCHESTRATOR INICIO {'(DRY-RUN)' if dry else ''} ===")
    log.info(f"hora atual BRT: {now_brt().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    snapshot_utc = datetime.now(timezone.utc)
    t0 = time.time()

    sucessos = falhas = pulados_deadline = 0
    for idx, (name, path, capt_args) in enumerate(CAPTADORES):
        # Deadline do bloco de captadores: se estourar, pula o resto e vai pro
        # matchmaking (que ainda tem a própria janela 07:00 BRT). Evita os runs
        # de 5h que matavam a janela inteira.
        elapsed = time.time() - t0
        if elapsed > CAPTADORES_DEADLINE_S:
            pulados_deadline = len(CAPTADORES) - idx
            log.warning(f"⊘ DEADLINE captadores ({CAPTADORES_DEADLINE_S}s) atingido após "
                        f"{elapsed:.0f}s — pulando {pulados_deadline} captadores restantes "
                        f"e seguindo pro matchmaking")
            break
        if rodar_captador(name, path, dry_run=dry, args=capt_args):
            sucessos += 1
        else:
            falhas += 1

    log.info(f"importers: {sucessos} sucesso, {falhas} falha"
             f"{f', {pulados_deadline} pulados (deadline)' if pulados_deadline else ''}")
    log.info(f"hora atual BRT (pós-importers): {now_brt().strftime('%H:%M:%S')}")

    # Recompute tier para obras sem classificacao_computed (lote 500/run).
    # Roda apos importers pra promover OURO/PRATA antes do matchmaking gerar matches.
    log.info("▶ Recompute tier obras sem classificacao…")
    t_rc = time.time()
    if dry:
        log.info("  [DRY] skip recompute_tier")
        log_captacao("RECOMPUTE_TIER", "pulado", erro="dry-run", dry_run=True)
    else:
        try:
            conn_rc = get_conn()
            cur_rc = conn_rc.cursor()
            cur_rc.execute("""
                SELECT id FROM obras
                WHERE classificacao_computed IS NULL
                  AND visivel = true
                LIMIT 500
            """)
            ids = [r[0] for r in cur_rc.fetchall()]
            for obra_id in ids:
                cur_rc.execute("SELECT recompute_classificacao_obra(%s)", (obra_id,))
            conn_rc.commit()
            cur_rc.close()
            conn_rc.close()
            dur_ms = int((time.time() - t_rc) * 1000)
            log.info(f"  ✓ RECOMPUTE_TIER: {len(ids)} obras em {dur_ms}ms")
            log_captacao("RECOMPUTE_TIER", "sucesso",
                         novos=len(ids), buscados=len(ids), duracao_ms=dur_ms)
        except Exception as e:
            dur_ms = int((time.time() - t_rc) * 1000)
            log.exception(f"  ✗ RECOMPUTE_TIER erro: {e}")
            log_captacao("RECOMPUTE_TIER", "erro",
                         erro=str(e)[:200], duracao_ms=dur_ms)

    # V2 (matches_v2) PRIMEIRO — é o engine canônico do painel de obras e roda
    # rápido (~2-3min). V1 (matches_obra_prestador → fornecedores) vem depois e
    # pode ser longo; rodar V2 antes garante que ele caiba na janela.
    rodar_matchmaking_v2(dry_run=dry)
    # V1 (legado) desativado pois 'matches_obra_prestador' agora é uma View dinâmica sobre 'matches_v2'
    # rodar_matchmaking(snapshot_utc, dry_run=dry)
    # rodar_regenerar_matches_prestadores(dry_run=dry)
    rodar_populador_sintetico(dry_run=dry)
    rodar_wire_in_decisores_empresa_alvo(dry_run=dry)
    rodar_enrichment_decisor_top_ouro(dry_run=dry)
    rodar_intel_obras_ouro(dry_run=dry)

    dur_ms = int((time.time() - t0) * 1000)
    _partes_final = []
    if falhas:
        _partes_final.append(f"{falhas} captadores falharam")
    if pulados_deadline:
        _partes_final.append(f"{pulados_deadline} pulados por deadline")
    erro_final = "; ".join(_partes_final) or None
    status_final = "sucesso" if not _partes_final else ("parcial" if pulados_deadline and not falhas else "erro")
    log_captacao("ORCHESTRATOR", status_final,
                 novos=sucessos, buscados=len(CAPTADORES),
                 erro=erro_final, duracao_ms=dur_ms, dry_run=dry)
    log.info(f"=== ORCHESTRATOR FIM em {dur_ms}ms (status={status_final}) ===")

    # Exit não-zero se algum captador falhou (pra cron capturar)
    return 0 if falhas == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
