#!/usr/bin/env bash
# bump-cache.sh — Atualiza version-bust nos <script src="/static/js/...?v=..."> de v2/index.html.
#
# Quando rodar:
#   Depois de editar QUALQUER arquivo em /app/frontend/static/js/* (v2-stores.js,
#   v2-helpers.js, v2-pages.js, v2-router.js, v2-admin.js) — antes de avisar Mari/users.
#   Browsers cacheiam JS por 1 dia (Cache-Control: max-age=86400); sem bump, F5 normal
#   serve cópia velha e a mudança só aparece após Ctrl+Shift+R.
#
# O que faz:
#   1. Calcula NEW_V = AAAAMMDD_HHMM (minuto atual).
#   2. Lê OLD_V do primeiro ?v=... no index.html.
#   3. docker exec sed -i s/?v=OLD/?v=NEW/g — bate em todas as 5 referências
#      (4 <script defer> + 1 document.write).
#   4. Reporta OLD → NEW + número de tags atualizadas.
#
# Idempotente: se rodar 2× no mesmo minuto, aborta com aviso (NEW=OLD).
# Não precisa restart de container: nginx não cacheia HTML v2 (servida do bind-mount).

set -euo pipefail

CONTAINER="wins_hub_v2-api-1"
INDEX_PATH="/app/frontend/index.html"
NEW_V=$(date +%Y%m%d_%H%M)

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "ERRO: container $CONTAINER não está rodando" >&2
  exit 1
fi

OLD_V=$(docker exec "$CONTAINER" grep -oE '\?v=[0-9_]+' "$INDEX_PATH" | head -1 | sed 's/?v=//') || true

if [ -z "${OLD_V:-}" ]; then
  echo "ERRO: nenhum ?v=... encontrado em $INDEX_PATH (frontend não tem version bust aplicado ainda)" >&2
  exit 1
fi

if [ "$OLD_V" = "$NEW_V" ]; then
  echo "AVISO: NEW ($NEW_V) == OLD ($OLD_V). Espere ≥1min ou bumpe manual." >&2
  exit 1
fi

docker exec "$CONTAINER" sed -i "s/?v=$OLD_V/?v=$NEW_V/g" "$INDEX_PATH"

COUNT=$(docker exec "$CONTAINER" grep -cE "\?v=$NEW_V" "$INDEX_PATH" || echo 0)

echo "✓ Cache bust aplicado em $CONTAINER:$INDEX_PATH"
echo "  $OLD_V → $NEW_V ($COUNT tags)"
echo ""
echo "Próximo visitante (incluindo Mari) com F5 normal já pega o JS novo."
