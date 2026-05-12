#!/bin/bash
# Wrapper captador notícias setoriais — cron sugerido a cada 6h.
# Inclui alerta por email (Resend) após 2 falhas consecutivas.
# Estado persistido em /root/wins_hub/var/captador_failures.

set -u  # erro em var não definida; NÃO usa -e (precisamos do exit code)

LOG_DIR="/var/log/wins_hub"
LOG_FILE="$LOG_DIR/captador_noticias_$(date +%Y%m%d_%H%M%S).log"
LOCK_FILE="/tmp/wins_hub_captador_noticias.lock"
STATE_DIR="/root/wins_hub/var"
FAIL_COUNT_FILE="$STATE_DIR/captador_failures"
ENV_FILE="/root/wins_hub/.env"
ALERT_TO="williamvnvn@gmail.com"
ALERT_THRESHOLD=2

mkdir -p "$LOG_DIR" "$STATE_DIR"

# Lock por PID
if [ -e "$LOCK_FILE" ]; then
    PID=$(cat "$LOCK_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Captador ja rodando (PID $PID). Saindo." | tee -a "$LOG_FILE"
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

echo "=== INICIO CAPTADOR NOTICIAS $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG_FILE"
docker exec wins_hub-api-1 python /app/scripts/captar_noticias_setoriais.py >> "$LOG_FILE" 2>&1
RESULT=$?
echo "=== FIM CAPTADOR NOTICIAS $(date '+%Y-%m-%d %H:%M:%S') (exit=$RESULT) ===" | tee -a "$LOG_FILE"

# Tracking de falhas consecutivas
CURRENT_FAIL=0
if [ -f "$FAIL_COUNT_FILE" ]; then
    CURRENT_FAIL=$(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0)
fi

if [ $RESULT -eq 0 ]; then
    # Sucesso — reset counter
    if [ "$CURRENT_FAIL" -gt 0 ]; then
        echo "Sucesso após $CURRENT_FAIL falha(s) consecutiva(s). Resetando contador." | tee -a "$LOG_FILE"
    fi
    echo 0 > "$FAIL_COUNT_FILE"
else
    NEW_FAIL=$((CURRENT_FAIL + 1))
    echo "$NEW_FAIL" > "$FAIL_COUNT_FILE"
    echo "Falha #$NEW_FAIL consecutiva (exit=$RESULT)." | tee -a "$LOG_FILE"

    if [ "$NEW_FAIL" -ge "$ALERT_THRESHOLD" ]; then
        echo "Threshold atingido ($NEW_FAIL >= $ALERT_THRESHOLD). Disparando alerta..." | tee -a "$LOG_FILE"

        # Carrega RESEND_API_KEY do .env
        if [ -f "$ENV_FILE" ]; then
            RESEND_API_KEY=$(grep -E "^RESEND_API_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
            RESEND_FROM=$(grep -E "^RESEND_FROM=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
            RESEND_FROM="${RESEND_FROM:-WiNS HUB <contato@winshubcomercial.com.br>}"
        else
            RESEND_API_KEY=""
        fi

        if [ -z "$RESEND_API_KEY" ]; then
            echo "RESEND_API_KEY ausente; alerta NAO enviado." | tee -a "$LOG_FILE"
        else
            # Tail das ultimas 30 linhas do log pra contexto
            LOG_TAIL=$(tail -n 30 "$LOG_FILE" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
            SUBJECT="[WiNS HUB] Captador notícias falhou $NEW_FAIL vezes consecutivas"
            HTML_BODY=$(cat <<HTML
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#0a0e1a;color:#e8ecf4;padding:20px;">
<h2 style="color:#f5b800;">⚠️ Captador notícias com falhas consecutivas</h2>
<p><b>Falhas:</b> $NEW_FAIL (threshold $ALERT_THRESHOLD)</p>
<p><b>Última execução:</b> $(date '+%Y-%m-%d %H:%M:%S %Z')</p>
<p><b>Exit code:</b> $RESULT</p>
<p><b>Log:</b> <code>$LOG_FILE</code></p>
<h3>Tail do log:</h3>
<pre style="background:#0f1525;padding:12px;border:1px solid #1f2942;border-radius:8px;font-size:11px;overflow-x:auto;">$LOG_TAIL</pre>
<p style="color:#6b7693;font-size:11px;">Contador será resetado após este alerta. Próxima execução começa do zero.</p>
</body></html>
HTML
)
            # Payload JSON via python pra escapar corretamente
            PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'from': '''$RESEND_FROM''',
    'to': ['$ALERT_TO'],
    'subject': '''$SUBJECT''',
    'html': sys.stdin.read()
}))
" <<< "$HTML_BODY")

            HTTP_CODE=$(curl -sS -o /tmp/resend_alerta_resp.json -w "%{http_code}" \
                -X POST "https://api.resend.com/emails" \
                -H "Authorization: Bearer $RESEND_API_KEY" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD")

            if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
                echo "Alerta enviado pra $ALERT_TO (HTTP $HTTP_CODE)." | tee -a "$LOG_FILE"
                # Reset counter pra nao spamar a cada 6h
                echo 0 > "$FAIL_COUNT_FILE"
            else
                echo "Falha ao enviar alerta (HTTP $HTTP_CODE): $(cat /tmp/resend_alerta_resp.json)" | tee -a "$LOG_FILE"
            fi
        fi
    fi
fi

# Cleanup logs >30d
find "$LOG_DIR" -name "captador_noticias_*.log" -mtime +30 -delete 2>/dev/null

exit $RESULT

# ============================================================================
# CRON ENTRY SUGERIDO — NÃO ATIVAR AINDA (rodar manual 3 dias 12-15/05)
# Frequência: a cada 6h
# Após validar manual, adicionar ao crontab root:
#   0 */6 * * * /root/wins_hub/scripts/cron_captar_noticias.sh >> /var/log/wins_hub_captador_cron.log 2>&1
# ============================================================================
