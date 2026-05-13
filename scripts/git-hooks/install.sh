#!/usr/bin/env bash
# WiNS Hub — instalador do git pre-commit hook.
# Cria symlink .git/hooks/pre-commit -> ../../scripts/git-hooks/pre-commit
# Idempotente — pode rodar varias vezes. Se ja existe outro hook, faz backup.

set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [ -z "$REPO_ROOT" ]; then
    echo "ERRO: nao parece ser um repo git." >&2
    exit 1
fi

HOOK_SRC="$REPO_ROOT/scripts/git-hooks/pre-commit"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-commit"

if [ ! -f "$HOOK_SRC" ]; then
    echo "ERRO: $HOOK_SRC nao existe." >&2
    exit 1
fi

# Garante permissao de execucao no source
chmod +x "$HOOK_SRC"

# Backup do hook anterior se nao for ja um symlink pra este
if [ -e "$HOOK_DST" ] || [ -L "$HOOK_DST" ]; then
    CUR=$(readlink "$HOOK_DST" 2>/dev/null || echo "")
    if [ "$CUR" = "../../scripts/git-hooks/pre-commit" ]; then
        echo "OK: symlink ja aponta para o hook versionado."
        exit 0
    fi
    BAK="$HOOK_DST.bak_$(date +%Y%m%d_%H%M%S)"
    echo "Backup do hook existente em $BAK"
    mv "$HOOK_DST" "$BAK"
fi

# Cria symlink relativo (sobrevive a move do repo)
ln -s ../../scripts/git-hooks/pre-commit "$HOOK_DST"
echo "OK: hook instalado."
echo "  $HOOK_DST -> ../../scripts/git-hooks/pre-commit"

# Smoke test (executa hook sem nada staged, deve sair 0)
if "$HOOK_DST" >/dev/null 2>&1; then
    echo "OK: smoke test do hook passou."
else
    echo "AVISO: hook retornou exit nao-zero em smoke (sem staged files)." >&2
fi
