"""
drain_queue.py — Worker do enrichment_queue (pipeline_ev 01062026)

Le pendentes da fila, prioridade por capex DESC, roda descobrir_via_search_engines
+ decisor_gate (mesma logica bucket1_5), INSERT decisor + recompute classificacao.

Uso:
  # Cron a cada 5min, drenar ate 5 obras:
  docker exec wins_hub-api-1 python /app/scripts/drain_queue.py --commit --batch 5

  # Dry-run:
  docker exec wins_hub-api-1 python /app/scripts/drain_queue.py --batch 5

  # Loop continuo (alternativa ao cron):
  docker exec wins_hub-api-1 python /app/scripts/drain_queue.py --commit --loop

Marker: registrado_por='drain_queue:v1:YYYYMMDD'
"""
import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

from sales_intelligence.camada3_decisores.linkedin_search import descobrir_via_search_engines
from sales_intelligence.camada3_decisores.pncp_publico_search import (
    descobrir_via_pncp_publico,
    is_orgao_publico,
)
from sales_intelligence.decisor_gate import decisor_inserivel


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "db"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "wins_hub"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

TODAY_TAG = datetime.now().strftime("%Y%m%d")
MARKER = f"drain_queue:v1:{TODAY_TAG}"
LOCK_KEY = int(os.getenv("DRAIN_QUEUE_LOCK_KEY", "26061001"))
STALE_RUNNING_MINUTES = int(os.getenv("DRAIN_QUEUE_STALE_MINUTES", "45"))
HARD_BATCH_MAX = int(os.getenv("DRAIN_QUEUE_HARD_BATCH_MAX", "2"))

# 16/06: obra de governo (contratante) NAO é o alvo — o decisor está na empresa
# executora (vencedor da licitação). Quando adjudicada, mira a executora; quando
# governo-contratante sem executor conhecido, pula (evita regenerar lixo).
GOVERNO_RE = (
    r"^(munic|prefeit|estado d|governo|secretaria|fundo (munic|estad)|"
    r"c[aâ]mara|tribunal|minist[eé]rio|cons[oó]rcio inter|defensoria|"
    r"assembleia|superintend|departamento estad|pol[ií]cia|instituto fed|autarquia)"
)
SKIP_GOV_SEM_EXEC = f"""
          AND NOT (
            COALESCE(NULLIF(o.empresa_executora,''),'') = ''
            AND (COALESCE(o.executora_status,'') = 'aguardando_adjudicacao'
                 OR COALESCE(o.empresa,'') ~* '{GOVERNO_RE}')
          )"""

PRIORIDADE_TIPO = [
    "SUPPLY_CHAIN", "GERENTE_SUPRIMENTOS", "GERENTE_COMPRAS",
    "GERENTE_PROJETOS", "GERENTE_INDUSTRIAL", "COORDENADOR_OBRAS",
    "COORDENADOR_MANUTENCAO", "GERENTE_ENGENHARIA",
    "ENGENHEIRO_MECANICO_CIVIL", "PROJETISTA",
]


def fetch_pending(cur, batch_size: int):
    cur.execute(
        f"""
        SELECT eq.id AS queue_id, eq.obra_id, eq.capex, eq.tentativas,
               COALESCE(NULLIF(o.empresa_executora,''), o.empresa) AS empresa,
               COALESCE(NULLIF(o.cnpj_executora,''), o.cnpj) AS cnpj,
               o.nome AS obra_nome, o.fonte AS obra_fonte
        FROM enrichment_queue eq
        JOIN obras o ON o.id = eq.obra_id
        WHERE eq.status = 'pending'
          AND eq.tentativas < eq.max_tentativas
          AND o.motivo_invisivel IS NULL
          {SKIP_GOV_SEM_EXEC}
          -- decisao 02/06/2026: gates pncp removidos. Toda obra que entra
          -- na queue deve ser enriquecida. NOT EXISTS decisor evita re-trabalho
          -- por tick (cada obra recebe 1 passada Mari). Capex baixo / PIPELINE
          -- nao bloqueiam mais — escolha de produto, custo Serper aceito.
          AND NOT EXISTS (
            SELECT 1 FROM decisores_obra d
            WHERE d.obra_id=o.id AND d.excluido_em IS NULL
          )
        ORDER BY eq.capex DESC NULLS LAST, eq.criado_em ASC
        LIMIT %s
        """,
        (batch_size,),
    )
    return cur.fetchall()


def mark_status(cur, queue_id: int, status: str, erro_msg: str | None = None,
                inc_tentativas: bool = False):
    if inc_tentativas:
        cur.execute(
            """UPDATE enrichment_queue
               SET status=%s, processado_em=NOW(),
                   tentativas=tentativas+1, erro_msg=%s
               WHERE id=%s""",
            (status, (erro_msg or '')[:500], queue_id),
        )
    else:
        cur.execute(
            """UPDATE enrichment_queue
               SET status=%s, processado_em=NOW(), erro_msg=%s
               WHERE id=%s""",
            (status, (erro_msg or '')[:500], queue_id),
        )


def acquire_worker_lock(cur) -> bool:
    """Evita cron concorrente quando um ciclo demora mais que o intervalo."""
    cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_KEY,))
    return bool(cur.fetchone()["locked"])


def reset_stale_running(cur, minutes: int) -> int:
    """Devolve para a fila itens marcados running por workers mortos."""
    if minutes <= 0:
        return 0
    cur.execute(
        """
        UPDATE enrichment_queue
           SET status='pending',
               processado_em=NULL,
               erro_msg=LEFT(CONCAT_WS(' | ', NULLIF(erro_msg, ''), %s), 500)
         WHERE status='running'
           AND processado_em IS NOT NULL
           AND processado_em < NOW() - (%s::text || ' minutes')::interval
        """,
        (f"stale_reset:{MARKER}", minutes),
    )
    return cur.rowcount


def processar_obra(conn, cur, row, commit: bool, max_buckets: int) -> str:
    """Processa 1 obra. Retorna status final ('done','skip','error')."""
    queue_id = row["queue_id"]
    obra_id = row["obra_id"]
    empresa = (row["empresa"] or "").strip()
    cnpj = row["cnpj"] or None
    obra_fonte = row.get("obra_fonte") or ""

    # Marcar running (transaction inicial)
    mark_status(cur, queue_id, 'running')
    if commit:
        conn.commit()

    if not empresa:
        mark_status(cur, queue_id, 'skip', 'empresa_vazia')
        if commit:
            conn.commit()
        return 'skip'

    # Descobrir decisores via tecnica Mari (106 cargos PT+EN, 11 buckets)
    try:
        decisores = descobrir_via_search_engines(empresa, cnpj=cnpj, max_buckets=max_buckets)
    except Exception as e:
        mark_status(cur, queue_id, 'pending', f'descobrir_err: {e!r}', inc_tentativas=True)
        if commit:
            conn.commit()
        return 'error'

    # Fallback pncp_publico: orgao publico ou fonte pncp%% sem decisor via Mari
    fonte_marker = 'tecnica_mari'
    if not decisores and (obra_fonte.startswith('pncp') or is_orgao_publico(empresa)):
        try:
            decisores = descobrir_via_pncp_publico(empresa, cnpj=cnpj, max_queries=3)
            if decisores:
                fonte_marker = 'pncp_publico_serper'
        except Exception as e:
            print(f"  [pncp_publico] falha: {e!r}")

    if not decisores:
        mark_status(cur, queue_id, 'skip', 'sem_decisores')
        if commit:
            conn.commit()
        return 'skip'

    # Ordenar por prioridade tipo_cargo
    def _ordem(d):
        try:
            return PRIORIDADE_TIPO.index(d.tipo_cargo or "")
        except ValueError:
            return 99
    decisores_ordenados = sorted(decisores, key=_ordem)

    inseridos = 0
    for dec in decisores_ordenados:
        if inseridos >= 2:
            break
        if not dec.nome_pessoa or not dec.cargo_raw:
            continue
        # 1o insert tem que ser prioritario
        if dec.tipo_cargo not in PRIORIDADE_TIPO and inseridos == 0:
            continue
        # Skip duplicado
        cur.execute(
            """SELECT 1 FROM decisores_obra
               WHERE obra_id=%s AND lower(nome)=lower(%s) AND excluido_em IS NULL LIMIT 1""",
            (obra_id, dec.nome_pessoa),
        )
        if cur.fetchone():
            continue
        # Gate
        permite, motivo = decisor_inserivel(cur, dec.nome_pessoa, dec.cargo_raw, empresa)
        if not permite:
            continue
        if commit:
            linkedin_url = (
                f"https://br.linkedin.com/in/{dec.linkedin_slug}"
                if dec.linkedin_slug else None
            )
            cur.execute(
                """
                INSERT INTO decisores_obra
                  (obra_id, nome, cargo, tipo_cargo, linkedin_url, fonte, registrado_por)
                VALUES (%s,%s,%s,%s,%s,%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (obra_id, dec.nome_pessoa, dec.cargo_raw, dec.tipo_cargo,
                 linkedin_url, fonte_marker, MARKER),
            )
            if cur.rowcount:
                inseridos += 1
        else:
            inseridos += 1

    if commit and inseridos > 0:
        # calcular confianca + recompute na obra
        cur.execute(
            """SELECT id FROM decisores_obra
               WHERE obra_id=%s AND excluido_em IS NULL AND registrado_por=%s""",
            (obra_id, MARKER),
        )
        for r in cur.fetchall():
            dec_id = r['id']
            cur.execute("SELECT calcular_confianca_match_v2(%s)", (dec_id,))
        cur.execute("SELECT recompute_classificacao_obra(%s)", (obra_id,))
    elif commit:
        # Fix gate_reject_ou_dup: obra ainda precisa classificar via capex+fonte_tipo
        # (BRONZE/PIPELINE) mesmo sem decisor novo. Sem isso fica NULL para sempre.
        cur.execute("SELECT recompute_classificacao_obra(%s)", (obra_id,))

    final_status = 'done' if inseridos > 0 else 'skip'
    erro = None if inseridos > 0 else 'gate_reject_ou_dup'
    mark_status(cur, queue_id, final_status, erro)
    if commit:
        conn.commit()
    return final_status


def run_once(batch_size: int, commit: bool, max_buckets: int):
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        if not acquire_worker_lock(cur):
            print("[drain_queue] outro worker ativo; saindo")
            return {'processed': 0, 'done': 0, 'skip': 0, 'error': 0, 'locked': 1}

        resetados = reset_stale_running(cur, STALE_RUNNING_MINUTES) if commit else 0
        if resetados:
            conn.commit()
            print(f"[drain_queue] resetados {resetados} itens running obsoletos")

        rows = fetch_pending(cur, batch_size)
        if not rows:
            print("[drain_queue] fila vazia")
            return {'processed': 0, 'done': 0, 'skip': 0, 'error': 0}

        print(f"[drain_queue] processando {len(rows)} obras (modo={'COMMIT' if commit else 'DRY-RUN'}, marker={MARKER})")
        stats = {'processed': len(rows), 'done': 0, 'skip': 0, 'error': 0}

        for i, row in enumerate(rows, 1):
            cap_mi = float(row["capex"] or 0) / 1e6
            print(f"  [{i}/{len(rows)}] {(row['empresa'] or '(no empresa)')[:30]:30} | R${cap_mi:7.1f}mi | ", end='', flush=True)
            try:
                status = processar_obra(conn, cur, row, commit, max_buckets)
            except Exception as e:
                status = 'error'
                try:
                    mark_status(cur, row['queue_id'], 'pending', f'fatal: {e!r}', inc_tentativas=True)
                    if commit:
                        conn.commit()
                except Exception:
                    conn.rollback()
                print(f"ERR {e!r}")
            else:
                stats[status] = stats.get(status, 0) + 1
                print(status)
            time.sleep(2)  # throttle

        return stats
    finally:
        cur.close()
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="persistir (default dry-run)")
    ap.add_argument("--batch", type=int, default=int(os.getenv("DRAIN_QUEUE_BATCH", "1")),
                    help="obras por ciclo (default env DRAIN_QUEUE_BATCH ou 1)")
    ap.add_argument("--max-buckets", type=int, default=int(os.getenv("DRAIN_QUEUE_MAX_BUCKETS", "3")),
                    help="max buckets pra descobrir_via_search_engines (default env DRAIN_QUEUE_MAX_BUCKETS ou 3)")
    ap.add_argument("--loop", action="store_true",
                    help="loop continuo (dorme entre ciclos)")
    ap.add_argument("--sleep-between", type=int, default=30,
                    help="segundos entre ciclos (loop)")
    ap.add_argument("--sleep-empty", type=int, default=120,
                    help="segundos quando fila vazia (loop)")
    args = ap.parse_args()
    if args.batch > HARD_BATCH_MAX:
        print(f"[drain_queue] batch {args.batch} reduzido para limite defensivo {HARD_BATCH_MAX}")
        args.batch = HARD_BATCH_MAX

    if args.loop:
        print(f"[drain_queue] loop iniciado (batch={args.batch})")
        while True:
            stats = run_once(args.batch, args.commit, args.max_buckets)
            if stats['processed'] == 0:
                time.sleep(args.sleep_empty)
            else:
                time.sleep(args.sleep_between)
    else:
        stats = run_once(args.batch, args.commit, args.max_buckets)
        print(f"[drain_queue] {stats}")


if __name__ == "__main__":
    main()
