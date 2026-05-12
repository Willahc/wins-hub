#!/bin/bash
set -e
LOG_DIR="/var/log/wins_hub"
LOG_FILE="$LOG_DIR/cvm_$(date +%Y%m%d_%H%M%S).log"
LOCK_FILE="/tmp/wins_hub_cvm.lock"
mkdir -p "$LOG_DIR"
if [ -e "$LOCK_FILE" ]; then
    PID=$(cat "$LOCK_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Captacao CVM ja rodando (PID $PID). Saindo." | tee -a "$LOG_FILE"
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT
echo "=== INICIO CVM $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG_FILE"
docker exec wins_hub-api-1 python /app/scripts/captar_cvm.py >> "$LOG_FILE" 2>&1
RESULT=$?
echo "=== FIM CVM $(date '+%Y-%m-%d %H:%M:%S') (exit=$RESULT) ===" | tee -a "$LOG_FILE"
find "$LOG_DIR" -name "cvm_*.log" -mtime +30 -delete
exit $RESULT
