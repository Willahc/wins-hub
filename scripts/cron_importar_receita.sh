#!/bin/bash
# Wrapper de execucao da importacao da Receita Federal
# Rodado pelo cron domingo 02h UTC
# Auto-detecta a pasta mais recente VALIDA do mirror (filtra placeholders vazios)

set -e

LOG_DIR="/var/log/wins_hub"
LOG_FILE="$LOG_DIR/receita_$(date +%Y%m%d_%H%M%S).log"
LOCK_FILE="/tmp/wins_hub_receita.lock"
MIRROR="https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos"

mkdir -p "$LOG_DIR"

# Detecta a pasta mais recente que tenha Estabelecimentos9.zip acessivel.
# Casa dos Dados as vezes cria entrada da pasta sem ter feito upload — filtra essas.
detect_latest_pasta() {
    local pastas pasta
    pastas=$(curl -s --max-time 30 "${MIRROR}/" \
        | grep -oE 'href="20[0-9]{2}-[0-9]{2}-[0-9]{2}/"' \
        | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' \
        | sort -ru \
        | head -5)
    for pasta in $pastas; do
        if curl -sfI --max-time 15 "${MIRROR}/${pasta}/Estabelecimentos9.zip" >/dev/null 2>&1; then
            echo "$pasta"
            return 0
        fi
    done
    return 1
}

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

# Auto-detecta pasta mais recente valida
echo "Detectando pasta mais recente no mirror..." | tee -a "$LOG_FILE"
PASTA=$(detect_latest_pasta || true)
if [ -z "$PASTA" ]; then
    echo "FALHA: nenhuma pasta valida detectada no mirror. Usando PASTA_DEFAULT do script." | tee -a "$LOG_FILE"
    PASTA_ARG=""
else
    echo "Pasta detectada: $PASTA" | tee -a "$LOG_FILE"
    PASTA_ARG="--pasta $PASTA"
fi

# Roda dentro do container (importacao completa)
docker exec wins_hub-api-1 python /app/scripts/importar_receita.py $PASTA_ARG >> "$LOG_FILE" 2>&1
RESULT=$?

echo "=== FIM $(date '+%Y-%m-%d %H:%M:%S') (exit=$RESULT) ===" | tee -a "$LOG_FILE"

# Limpeza: apaga logs com mais de 30 dias
find "$LOG_DIR" -name "receita_*.log" -mtime +30 -delete

exit $RESULT
