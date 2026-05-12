#!/bin/bash
# Wrapper do orchestrator — executa diariamente às 02:00 via cron.
# Mesmo padrão dos outros wrappers: lock por PID, log datado, cleanup >30 dias.
set -e

LOG_DIR="/var/log/wins_hub"
LOG_FILE="$LOG_DIR/orchestrator_$(date +%Y%m%d_%H%M%S).log"
LOCK_FILE="/tmp/wins_hub_orchestrator.lock"

mkdir -p "$LOG_DIR"

if [ -e "$LOCK_FILE" ]; then
    PID=$(cat "$LOCK_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Orchestrator ja rodando (PID $PID). Saindo." | tee -a "$LOG_FILE"
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

echo "=== INICIO ORCHESTRATOR $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG_FILE"
docker exec wins_hub-api-1 python /app/scripts/orchestrator.py >> "$LOG_FILE" 2>&1
RESULT=$?
echo "=== FIM ORCHESTRATOR $(date '+%Y-%m-%d %H:%M:%S') (exit=$RESULT) ===" | tee -a "$LOG_FILE"

# Cleanup de logs antigos (>30 dias)
find "$LOG_DIR" -name "orchestrator_*.log" -mtime +30 -delete

exit $RESULT
