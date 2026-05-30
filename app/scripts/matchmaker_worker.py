"""V0.8.1 — Matchmaker worker v2 (FIX A+B+D+E).

Reescreve worker pra:
- FIX A: invoca calcular_score_match_v2 + INSERT batch em matches_v2 (era gerar_matches v1)
- FIX B: modo full|incremental (argv[2])
- FIX D: heartbeat thread daemon (UPDATE a cada 10s)
- FIX E: filtros porte != MICRO + uf_proximidade + LIMIT 200 candidatos/obra

Uso: python matchmaker_worker.py <job_id> [modo]
"""
import os
import sys
import signal
import threading
import time
import traceback
import psycopg2
from psycopg2.extras import RealDictCursor

if len(sys.argv) < 2:
    print('uso: matchmaker_worker.py <job_id> [modo]', file=sys.stderr)
    sys.exit(2)
JOB_ID = sys.argv[1]
MODO = (sys.argv[2] if len(sys.argv) > 2 else 'incremental').lower()
if MODO not in ('full', 'incremental'):
    print(f'modo invalido: {MODO}; usando incremental', file=sys.stderr)
    MODO = 'incremental'

DB_KW = dict(
    host=os.environ.get('DB_HOST', 'db'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', 'WiNS@Hub2026!'),
    dbname=os.environ.get('DB_NAME', 'wins_hub'),
)
CANDIDATES_LIMIT = int(os.environ.get('MATCHMAKER_CANDIDATES_LIMIT', '200'))
SCORE_MIN = int(os.environ.get('MATCHMAKER_SCORE_MIN', '30'))
HEARTBEAT_INTERVAL = 10

conn = psycopg2.connect(**DB_KW)
conn.autocommit = True

# heartbeat thread (FIX D) — conexão própria
_stop_heartbeat = threading.Event()

def _heartbeat_loop():
    hb_conn = psycopg2.connect(**DB_KW)
    hb_conn.autocommit = True
    try:
        while not _stop_heartbeat.is_set():
            try:
                with hb_conn.cursor() as cur:
                    cur.execute("UPDATE matchmaker_jobs SET heartbeat=NOW() WHERE id=%s", (JOB_ID,))
            except Exception as e:
                print(f'[hb {JOB_ID}] erro: {e}', flush=True)
                try: hb_conn.close()
                except Exception: pass
                try:
                    hb_conn = psycopg2.connect(**DB_KW)
                    hb_conn.autocommit = True
                except Exception: pass
            _stop_heartbeat.wait(HEARTBEAT_INTERVAL)
    finally:
        try: hb_conn.close()
        except Exception: pass

threading.Thread(target=_heartbeat_loop, daemon=True, name='hb').start()


def checkpoint(obra_id=None, **kwargs):
    if not kwargs and obra_id is None:
        return
    sets = ', '.join(f"{k}=%s" for k in kwargs)
    with conn.cursor() as cur:
        if obra_id:
            cur.execute(f"UPDATE matchmaker_jobs SET {sets}, ultimo_obra_id=%s WHERE id=%s",
                        (*kwargs.values(), obra_id, JOB_ID))
        else:
            cur.execute(f"UPDATE matchmaker_jobs SET {sets} WHERE id=%s",
                        (*kwargs.values(), JOB_ID))


def status_atual():
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM matchmaker_jobs WHERE id=%s", (JOB_ID,))
        row = cur.fetchone()
    return row[0] if row else None


def finalizar(status, erro=None):
    sets = "status=%s, finalizado_em=NOW()"
    args = [status]
    if erro:
        sets += ", erro=%s"
        args.append(erro)
    args.append(JOB_ID)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE matchmaker_jobs SET {sets} WHERE id=%s", tuple(args))
    _stop_heartbeat.set()


def handle_term(*_):
    print(f'[worker {JOB_ID}] SIGTERM recebido, salvando PAUSADO', flush=True)
    finalizar('PAUSADO', erro='SIGTERM')
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_term)


# obras-alvo
if MODO == 'full':
    obras_sql = """
        SELECT o.id::text AS id, o.setor, o.uf
        FROM obras o
        WHERE o.visivel=true
          AND o.classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
          AND (COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA' OR o.validacao_obra_at IS NOT NULL)
          AND o.setor IS NOT NULL AND o.uf IS NOT NULL
          AND o.fase NOT IN ('PIPELINE','CONCLUIDA')
        ORDER BY
          CASE o.classificacao_computed
            WHEN 'OURO' THEN 1 WHEN 'PRATA' THEN 2
            WHEN 'BRONZE' THEN 3 ELSE 4 END,
          o.valor_estimado DESC NULLS LAST, o.id
    """
else:
    obras_sql = """
        SELECT o.id::text AS id, o.setor, o.uf
        FROM obras o
        WHERE o.visivel=true
          AND o.classificacao_computed IN ('OURO','PRATA','BRONZE','PIPELINE')
          AND (COALESCE(o.fonte_tipo,'OFICIAL') != 'NOTICIA' OR o.validacao_obra_at IS NOT NULL)
          AND o.setor IS NOT NULL AND o.uf IS NOT NULL
          AND o.fase NOT IN ('PIPELINE','CONCLUIDA')
          AND NOT EXISTS (SELECT 1 FROM matches_v2 m WHERE m.obra_id=o.id)
        ORDER BY
          CASE o.classificacao_computed
            WHEN 'OURO' THEN 1 WHEN 'PRATA' THEN 2
            WHEN 'BRONZE' THEN 3 ELSE 4 END,
          o.valor_estimado DESC NULLS LAST, o.id
    """

cur = conn.cursor(cursor_factory=RealDictCursor)
cur.execute(obras_sql)
obras = cur.fetchall()
cur.close()

print(f'[worker {JOB_ID}] modo={MODO} {len(obras)} obras-alvo cap={CANDIDATES_LIMIT} score_min={SCORE_MIN}', flush=True)
checkpoint(obras_alvo=len(obras), modo=MODO)


# query única por obra: top N candidatos peso scc*up, engine v2 LATERAL, INSERT ON CONFLICT
MATCH_SQL = """
INSERT INTO matches_v2 (obra_id, cnpj, score, score_breakdown, gerado_em)
SELECT %(obra_id)s::uuid, c.cnpj, m.score, m.breakdown, NOW()
FROM (
  SELECT f.cnpj, MAX(scc.peso) * MAX(up.peso) AS pre_score
  FROM fornecedores f
  JOIN setor_cnae_compatibility scc
    ON scc.setor_obra = %(setor)s
   AND (f.cnae_principal = scc.cnae_codigo OR scc.cnae_codigo = ANY(f.cnae_secundarios))
  JOIN uf_proximidade up ON up.uf_obra = %(uf)s AND up.uf_fornec = f.uf
  WHERE f.porte_inferido != 'MICRO'
    AND f.razao_social IS NOT NULL AND TRIM(f.razao_social) != ''
  GROUP BY f.cnpj
  ORDER BY pre_score DESC
  LIMIT %(lim)s
) c
CROSS JOIN LATERAL calcular_score_match_v2(%(obra_id)s::uuid, c.cnpj) m
WHERE m.score >= %(score_min)s
ON CONFLICT (obra_id, cnpj) DO UPDATE
  SET score = EXCLUDED.score,
      score_breakdown = EXCLUDED.score_breakdown,
      gerado_em = NOW()
"""

total_matches = 0
i = 0
try:
    for i, obra in enumerate(obras):
        s = status_atual()
        if s in ('PAUSADO', 'CONCLUIDO'):
            print(f'[worker {JOB_ID}] status={s} detectado, encerrando em {i}/{len(obras)}', flush=True)
            break
        obra_id = obra['id']
        try:
            t0 = time.time()
            with conn.cursor() as cur:
                cur.execute(MATCH_SQL, {
                    'obra_id': obra_id, 'setor': obra['setor'], 'uf': obra['uf'],
                    'lim': CANDIDATES_LIMIT, 'score_min': SCORE_MIN,
                })
                inserted = cur.rowcount
            total_matches += inserted
            dt = time.time() - t0
            if dt > 30:
                print(f'[worker {JOB_ID}] obra {obra_id} {obra["setor"]}/{obra["uf"]} +{inserted} em {dt:.1f}s', flush=True)
        except Exception as e:
            print(f'[worker {JOB_ID}] erro obra {obra_id}: {e}', flush=True)
        if (i + 1) % 10 == 0:
            checkpoint(obra_id, obras_processadas=i + 1, matches_criados=total_matches)
            print(f'[worker {JOB_ID}] {i+1}/{len(obras)} {total_matches} matches', flush=True)
    else:
        checkpoint(obras[-1]['id'] if obras else None, obras_processadas=len(obras), matches_criados=total_matches)
        finalizar('CONCLUIDO')
        print(f'[worker {JOB_ID}] CONCLUIDO {len(obras)} obras {total_matches} matches', flush=True)
        sys.exit(0)

    checkpoint(obras_processadas=i, matches_criados=total_matches)
    with conn.cursor() as cur:
        cur.execute("UPDATE matchmaker_jobs SET finalizado_em=NOW() WHERE id=%s AND finalizado_em IS NULL", (JOB_ID,))
    _stop_heartbeat.set()
except Exception as e:
    tb = traceback.format_exc()
    print(f'[worker {JOB_ID}] erro fatal: {tb}', flush=True)
    finalizar('ERRO', erro=str(e)[:500])
    sys.exit(1)
