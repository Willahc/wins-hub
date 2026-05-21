# Contributing — WiNS Hub

Notas para evitar regressões conhecidas.

## 🚨 Anti-regressão: admin prompt em landing anônima

**Histórico:** 3 regressões em 4 dias (20-21/05/2026). `window.prompt("Admin token:")` vazou em landing anônima cada vez que `_token()` voltou a ser chamado fora de `/admin*`.

**Arquitetura atual (a partir de 21/05):**

1. **Camada 1 — Carregamento condicional** (`index.html` linha ~18-32): o script `v2-admin.js` só é injetado se `location.pathname.startsWith("/admin")`. Fora disso, stubs no-op de `adminPage()` / `adminVendasPage()` são instalados.
2. **Camada 1b — `<template x-if>`** ao redor das `<section>` admin e admin-vendas: Alpine não cria o componente quando a rota não é admin, então `x-init="carregar()"` jamais executa em landing.
3. **Camada 2 — Guard em `carregar()`** dentro de `v2-admin.js`: early-return se `!pathname.startsWith("/admin")`.
4. **Camada 3 — Guard em `_token()`**: early-return se `!pathname.startsWith("/admin")`.

**Regras vinculantes para quem mexer no frontend v2:**

- **Não converter** `<template x-if="$store.nav.rota === admin">` para `x-show` (Alpine `x-show` não impede `x-init`; é só CSS `display:none`).
- **Não mover** o callsite `window.prompt("Admin token:")` para fora de `v2-admin.js` (atualmente em `_token()`). Outros arquivos JS não podem chamar `prompt()` de jeito nenhum.
- **Não remover** o gate de pathname no `<head>` do `index.html` que carrega `v2-admin.js` condicionalmente.
- **Não substituir** os stubs `window.adminPage = function() { return {...} }` por nada que faça fetch ou prompt.

**Como verificar antes de commitar:**

```bash
# Estático (rápido, não precisa container)
./scripts/smoke_test_admin_prompt.sh

# Manual em aba anônima:
# 1. Abrir Chrome incognito, F12 → Console
# 2. Visitar / , /obras, /fornecedores, /como-funciona, /planos, /login
# 3. Esperado: nenhum modal de prompt, console limpo, zero fetch para /api/admin/*
# 4. Só /admin deve disparar o prompt
```

O `pre-commit` (em `scripts/git-hooks/pre-commit`, symlinked em `.git/hooks/`) executa o smoke test automaticamente quando arquivos em `wins_hub_v2/app/frontend` mudam. Falha = commit bloqueado.

**Lições aprendidas:**

- Alpine.js `x-init` dispara no parse do HTML, **independente** de `x-show`. Não use `x-show` para gatear código que tem side effects (prompt, fetch, etc.).
- `localStorage` mascara bugs de prompt: dev com token cacheado nunca vê o problema. Sempre testar em aba anônima nova.
- 2 sessões Claude editando o mesmo `main.py`/HTML em paralelo clobberam edits silenciosamente. Verificar `ps -ef | grep claude` antes de mexer.

## Layout dos JS frontend v2

- `v2-stores.js`: Alpine stores (auth, nav, filters, modal, toasts).
- `v2-helpers.js`: helpers globais (`abrirCadastro`, `aplicarSetor`, formatadores).
- `v2-pages.js`: páginas públicas/logadas (landing, dashboard, obra, fornecedor, perfil, vendas, contatos, ranking, planos, login, esqueci, reset, primeiro-acesso, cadastro modal). **NÃO COLOCAR** código admin aqui — vai pra `v2-admin.js`.
- `v2-admin.js`: `adminPage()` + `adminVendasPage()`. Carregado condicionalmente. Único arquivo com `window.prompt()`.
- `v2-router.js`: SPA routing.
