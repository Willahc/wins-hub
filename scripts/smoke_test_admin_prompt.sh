#!/usr/bin/env bash
# WiNS Hub — Smoke test anti-regressão prompt admin
# ════════════════════════════════════════════════════════════════════════════
# Verifica que window.prompt("Admin token:") JAMAIS vaza em rotas públicas.
# 3 regressões registradas (sessões 20-21/05) — esta gate evita a 4ª.
#
# Uso:
#   ./scripts/smoke_test_admin_prompt.sh            (testa produção)
#   ./scripts/smoke_test_admin_prompt.sh local      (testa container local)
# Exit 0 = passou; exit 1 = bloqueio.
# ════════════════════════════════════════════════════════════════════════════
set -u

MODE="${1:-prod}"
case "$MODE" in
  prod)  BASE="https://winshubcomercial.com.br" ;;
  local) BASE="http://127.0.0.1:8001" ;;
  *) echo "Uso: $0 [prod|local]"; exit 2 ;;
esac

ROTAS_PUBLICAS=("/" "/obras" "/fornecedores" "/como-funciona" "/planos" "/login")
NEEDLES=("Admin token" "wnshub_admin_token")

echo "▶ smoke test admin-prompt em $BASE"
FAIL=0

for rota in "${ROTAS_PUBLICAS[@]}"; do
  body=$(curl -fsS --max-time 8 "$BASE$rota" 2>/dev/null || echo "")
  if [ -z "$body" ]; then
    echo "  ⚠ $rota — sem resposta (skip)"
    continue
  fi
  hit_count=0
  for needle in "${NEEDLES[@]}"; do
    n=$(printf "%s" "$body" | grep -c "$needle" || true)
    if [ "$n" -gt 0 ]; then
      echo "  ✗ $rota — encontrou \"$needle\" ($n ocorrências)"
      hit_count=$((hit_count + n))
    fi
  done
  if [ "$hit_count" -eq 0 ]; then
    echo "  ✓ $rota"
  else
    FAIL=$((FAIL + hit_count))
  fi
done

# Verificação estática: prompt() só pode aparecer em v2-admin.js
FRONTEND="/root/wins_hub_v2/app/frontend/static/js"
if [ -d "$FRONTEND" ]; then
  echo "▶ check estático em $FRONTEND"
  # Match prompt( fora de v2-admin.js, IGNORANDO comentários JS:
  #   - linhas começando com // (single-line comments)
  #   - linhas com /* ... prompt( ... */ (block comments inline)
  # Real callsite é `const x = prompt(...)` ou `prompt(...);` — não menção em comentário.
  leak=$(grep -rHn "prompt(" "$FRONTEND" 2>/dev/null \
    | grep -v "\.bak" \
    | grep -v "v2-admin.js" \
    | grep -vE ":[[:space:]]*//" \
    | grep -vE ":.*/\*.*prompt\(" \
    || true)
  if [ -n "$leak" ]; then
    echo "  ✗ prompt() encontrado fora de v2-admin.js (não-comentário):"
    echo "$leak" | sed "s/^/      /"
    FAIL=$((FAIL + 1))
  else
    echo "  ✓ prompt() só em v2-admin.js (menções em comentários OK)"
  fi

  # Verifica que index.html carrega v2-admin.js condicionalmente
  if [ -f "/root/wins_hub_v2/app/frontend/index.html" ]; then
    if grep -q "v2-admin.js" /root/wins_hub_v2/app/frontend/index.html; then
      if grep -q "pathname.indexOf.*admin" /root/wins_hub_v2/app/frontend/index.html; then
        echo "  ✓ v2-admin.js carregado condicionalmente em /admin*"
      else
        echo "  ✗ v2-admin.js no index.html mas SEM gate de pathname"
        FAIL=$((FAIL + 1))
      fi
    fi
  fi
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "✗ FALHOU — $FAIL violações detectadas"
  echo "  REGRA: window.prompt(\"Admin token:\") jamais pode vazar em rotas públicas."
  echo "  Arquitetura: v2-admin.js só carrega em /admin* (gate em index.html)."
  exit 1
fi

echo "✓ PASSOU — nenhum vazamento de admin-prompt detectado"
exit 0
