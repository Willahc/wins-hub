#!/bin/bash
# WiNS Hub — Orchestrator Wrapper Protegido (V9 14/05/2026)
#
# Protege contra: execucao dupla, container morto, travamentos, falha silenciosa.
# Substitui cron_orchestrator.sh (preservado como fallback).
#
# Cron sugerido (preserva schedule diario 02:00 BRT da janela matchmaking):
#   0 5 * * * /root/wins_hub/scripts/run_orchestrator.sh
#
# NAO usar set -e: precisamos capturar exit code do timeout/docker exec.
set -u
set -o pipefail

LOCK_FILE="/tmp/wins_hub_orchestrator.lock"
LOG_DIR="/var/log/wins_hub"
LOG_FILE="${LOG_DIR}/orchestrator_$(date +%Y%m%d_%H%M%S).log"
TIMEOUT_SECONDS=5400  # 1.5h — aumentado 09/06 pra suportar catch-ups multi-dia
CONTAINER="wins_hub-api-1"
DB_CONTAINER="wins_hub-db-1"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ── 1. LOCK ────────────────────────────────────────────────────────────────
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "0")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "SKIP: orchestrator ja rodando (PID $OLD_PID)"
        exit 0
    fi
    log "WARN: lock orfao encontrado (PID $OLD_PID morto). Removendo."
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "CLEANUP: lock removido (exit_signal)"' EXIT

# ── 2. CONTAINER CHECK ─────────────────────────────────────────────────────
if ! sudo docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    log "ERROR: container $CONTAINER nao esta rodando. Tentando start..."
    sudo docker start "$CONTAINER" 2>&1 | tee -a "$LOG_FILE" || true
    sleep 15
    if ! sudo docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        log "FATAL: container $CONTAINER nao subiu. Abortando."
        exit 1
    fi
    log "INFO: container $CONTAINER reiniciado com sucesso."
fi

# ── 3. EXECUTAR COM TIMEOUT ────────────────────────────────────────────────
log "START: orchestrator iniciando (timeout=${TIMEOUT_SECONDS}s)"
START_TIME=$(date +%s)

timeout "$TIMEOUT_SECONDS" sudo docker exec "$CONTAINER" \
    python /app/scripts/orchestrator.py \
    >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

case $EXIT_CODE in
    0)   log "SUCCESS: orchestrator concluido em ${ELAPSED}s" ;;
    124) log "ERROR: orchestrator TIMEOUT apos ${ELAPSED}s (limite ${TIMEOUT_SECONDS}s)" ;;
    137) log "ERROR: orchestrator OOM-killed em ${ELAPSED}s" ;;
    *)   log "ERROR: orchestrator exit=$EXIT_CODE em ${ELAPSED}s" ;;
esac

# ── 4. HEALTH CHECK (4h window p/ scheduled runs) ──────────────────────────
OBRAS_4H=$(sudo docker exec "$DB_CONTAINER" psql -U postgres -d wins_hub -tAc \
    "SELECT COUNT(*) FROM obras WHERE criado_em >= NOW() - INTERVAL '4 hours'" 2>/dev/null || echo "?")
log "HEALTH: obras inseridas nas ultimas 4h: $OBRAS_4H"

# ── 5. ROTACAO DE LOGS (>30 dias) ──────────────────────────────────────────
find "$LOG_DIR" -name "orchestrator_*.log" -mtime +30 -delete 2>/dev/null || true

log "DONE: wrapper finalizado"
exit $EXIT_CODE
