#!/bin/bash
# Wrapper de execucao da importacao da Receita Federal
# Rodado pelo cron toda segunda 02h da manha

set -e

LOG_DIR="/var/log/wins_hub"
LOG_FILE="$LOG_DIR/receita_$(date +%Y%m%d_%H%M%S).log"
LOCK_FILE="/tmp/wins_hub_receita.lock"

mkdir -p "$LOG_DIR"

# Lock - evita 2 execucoes em paralelo
if [ -e "$LOCK_FILE" ]; then
    PID=$(cat "$LOCK_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Importacao ja rodando (PID $PID). Saindo." | tee -a "$LOG_FILE"
        exit 1
    fi
    echo "Lock antigo encontrado mas processo nao existe. Removendo." >> "$LOG_FILE"
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

echo "=== INICIO $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG_FILE"

# Roda dentro do container (importacao completa)
docker exec wins_hub-api-1 python /app/scripts/importar_receita.py >> "$LOG_FILE" 2>&1
RESULT=$?

echo "=== FIM $(date '+%Y-%m-%d %H:%M:%S') (exit=$RESULT) ===" | tee -a "$LOG_FILE"

# Limpeza: apaga logs com mais de 30 dias
find "$LOG_DIR" -name "receita_*.log" -mtime +30 -delete

exit $RESULT
