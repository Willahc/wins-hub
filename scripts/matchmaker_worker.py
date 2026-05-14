"""V0.1.6 — Matchmaker worker on-demand.
Processa obras sem match. Verifica flag status a cada N obras pra
permitir pause/stop graceful.

Uso: python matchmaker_worker.py <job_id>

Lê:
- DB_HOST/USER/PASSWORD/NAME do env
- matchmaker_jobs.status: se PAUSADO/CONCLUIDO → break

Escreve:
- matchmaker_jobs.{obras_processadas, matches_criados, ultimo_obra_id,
  status, finalizado_em, erro}
"""
import os
import sys
import signal
import traceback
import psycopg2
from psycopg2.extras import RealDictCursor

if len(sys.argv) < 2:
    print('uso: matchmaker_worker.py <job_id>', file=sys.stderr)
    sys.exit(2)
JOB_ID = sys.argv[1]

DB_KW = dict(
    host=os.environ.get('DB_HOST', 'db'),
    user=os.environ.get('DB_USER', 'postgres'),
    password=os.environ.get('DB_PASSWORD', 'WiNS@Hub2026!'),
    dbname=os.environ.get('DB_NAME', 'wins_hub'),
)

conn = psycopg2.connect(**DB_KW)
conn.autocommit = True

def checkpoint(obra_id=None, **kwargs):
    if not kwargs and obra_id is None:
        return
    sets = ', '.join(f"{k}=%s" for k in kwargs)
    cur = conn.cursor()
    if obra_id:
        cur.execute(f"UPDATE matchmaker_jobs SET {sets}, ultimo_obra_id=%s WHERE id=%s",
                    (*kwargs.values(), obra_id, JOB_ID))
    else:
        cur.execute(f"UPDATE matchmaker_jobs SET {sets} WHERE id=%s",
                    (*kwargs.values(), JOB_ID))
    cur.close()

def status_atual():
    cur = conn.cursor()
    cur.execute("SELECT status FROM matchmaker_jobs WHERE id=%s", (JOB_ID,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None

def finalizar(status, erro=None):
    sets = "status=%s, finalizado_em=NOW()"
    args = [status]
    if erro:
        sets += ", erro=%s"
        args.append(erro)
    args.append(JOB_ID)
    cur = conn.cursor()
    cur.execute(f"UPDATE matchmaker_jobs SET {sets} WHERE id=%s", tuple(args))
    cur.close()

def handle_term(*_):
    print(f'[worker {JOB_ID}] SIGTERM recebido, salvando PAUSADO', flush=True)
    finalizar('PAUSADO', erro='SIGTERM')
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_term)

# Import the matchmaking function.
# /app/services/matchmaking.py inside container; /root/wins_hub/app/services on host.
sys.path.insert(0, '/app')
sys.path.insert(0, '/root/wins_hub')
sys.path.insert(0, '/root/wins_hub/app')
try:
    from services.matchmaking import gerar_matches_para_obra
except ImportError:
    from app.services.matchmaking import gerar_matches_para_obra  # type: ignore

# Lista obras-alvo: sem match canônico, visíveis, com setor+uf (pré-req da fn)
cur = conn.cursor(cursor_factory=RealDictCursor)
cur.execute("""
    SELECT o.id::text AS id FROM obras o
    WHERE o.visivel=true
      AND o.setor IS NOT NULL AND o.uf IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM matches_obra_prestador m WHERE m.obra_id=o.id)
    ORDER BY o.valor_estimado DESC NULLS LAST, o.id
""")
obras = [r['id'] for r in cur.fetchall()]
cur.close()

print(f'[worker {JOB_ID}] {len(obras)} obras-alvo identificadas', flush=True)
checkpoint(obras_alvo=len(obras))

total_matches = 0
try:
    for i, obra_id in enumerate(obras):
        # Checa flag a cada iteração (controla pause)
        s = status_atual()
        if s in ('PAUSADO', 'CONCLUIDO'):
            print(f'[worker {JOB_ID}] status={s} detectado, encerrando em obra {i}/{len(obras)}', flush=True)
            break
        try:
            stats = gerar_matches_para_obra(obra_id)
            total_matches += int(stats.get('matches_gerados') or 0)
        except Exception as e:
            print(f'[worker {JOB_ID}] erro obra {obra_id}: {e}', flush=True)
        # Checkpoint a cada 10
        if (i + 1) % 10 == 0:
            checkpoint(obra_id, obras_processadas=i + 1, matches_criados=total_matches)
            print(f'[worker {JOB_ID}] {i+1}/{len(obras)} · {total_matches} matches', flush=True)
    else:
        # Loop terminou normalmente
        checkpoint(obras[-1] if obras else None, obras_processadas=len(obras), matches_criados=total_matches)
        finalizar('CONCLUIDO')
        print(f'[worker {JOB_ID}] CONCLUIDO · {len(obras)} obras · {total_matches} matches', flush=True)
        sys.exit(0)

    # Saiu do loop por break (PAUSADO/CONCLUIDO detectado)
    checkpoint(obras_processadas=i, matches_criados=total_matches)
    # Status já é PAUSADO; só atualiza finalizado_em se ainda não tem
    cur = conn.cursor()
    cur.execute("UPDATE matchmaker_jobs SET finalizado_em=NOW() WHERE id=%s AND finalizado_em IS NULL", (JOB_ID,))
    cur.close()
except Exception as e:
    tb = traceback.format_exc()
    print(f'[worker {JOB_ID}] erro fatal: {tb}', flush=True)
    finalizar('ERRO', erro=str(e)[:500])
    sys.exit(1)
