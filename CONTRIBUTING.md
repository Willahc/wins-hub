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

---

## Decisões arquiteturais — sprint v0.5-v0.7 (21/05/2026)

### v2 frontend é monolito unificado via SPA
Todos os 5 HTMLs públicos (`index.html`, `login.html`, `esqueci.html`, `reset.html`, `score.html`) são clones do `index.html`. Rotas client-side via `nav.rota` (em `v2-router.js`) + sections gated por `x-show`. Mudanças no master devem ser propagadas pros 4 derivados (`cp index.html` sobre eles). Inconsistência causou 33 console errors em /login na sprint v0.7.

### Wrappers `this._post()` em vez de fetch direto
Endpoints admin de write (`criar-representante`, `comissoes/marcar-paga`, etc.) usam wrapper `this._post()` em `v2-admin.js`. Auditorias devem grep AMBOS `fetch(` e `this._post(` para não dar falso positivo de "não usado".

### Polling visibility-aware é padrão
Qualquer polling (matchmaker status, matches/status, pagamento status) usa o pattern:
```js
setInterval(() => {
  if (document.visibilityState === visible) this._tick();
}, 5000);
```
Pausa quando aba inativa, retoma na volta. Sem cap de tentativas — quem fica responsável por parar é o callback (transição de status pra terminal).

### LEGACY filtra nivel1_* (workflow Mari)
5 endpoints (`/api/dashboard/{matches,times}_ouro`, `/api/admin/{ouro_parcial,empresas_ouro_gaps,em_execucao_sem_decisor}`) filtram por `nivel1_nome + nivel1_email/linkedin + cargo_decisor_keyword`. Semântica = "obra com decisor preenchido + contato verificado", NÃO igual ao TIER canônico (que filtra por CAPEX). Sentinelas `# LEGACY INTENCIONAL` documentam decisão. Não migrar.

### Snapshot canônico via query referência (não números no código)
Contagens por tier mudam diariamente. Não codar `224 OURO` em lugar nenhum. Usar query:
```sql
SELECT classificacao_computed, COUNT(*) FROM obras
WHERE (visivel IS NULL OR visivel=true)
  AND COALESCE(fonte_tipo,OFICIAL) != NOTICIA
GROUP BY 1;
```

## Como rodar local (deploy ref)

Stack: docker-compose. Containers principais:
- `wins_hub-db-1` (Postgres) — bind mount `/var/lib/postgresql/data`
- `wins_hub-api-1` (FastAPI v1) — bind mount `/root/wins_hub/app` → `/app`
- `wins_hub_v2-api-1` (FastAPI + v2 frontend) — bind mount `/root/wins_hub/app` → `/app` E `/root/wins_hub_v2/app/frontend` → `/app/frontend`
- `wins_hub-nginx-1` (nginx :443/:80) — proxy_pass `http://wins_hub_v2-api-1:8000`

Editar frontend = direto em `/root/wins_hub_v2/app/frontend/` (sem rebuild; bind mount serve fresh). Editar backend Python = `docker restart wins_hub_v2-api-1` pra picar mudanças (sem hot-reload em produção).

## Rollback

Tags semânticas (v0.X.Y-descricao). Pra reverter feature:
```bash
cd /root/wins_hub_v2  # ou /root/wins_hub
git log --oneline -20         # achar commit antes da feature
git checkout <hash> -- <file> # cherry-pick reverso
# OU
git revert <hash>             # cria commit reverso
docker restart wins_hub_v2-api-1   # se mexeu Python
```

Backups pg_dump em `/root/backups/wins_hub_YYYYMMDD_HHMMSS.sql.gz` (retenção 7 dias local + GDrive permanente via `rclone sync`). Restore:
```bash
zcat /root/backups/wins_hub_TIMESTAMP.sql.gz | docker exec -i wins_hub-db-1 psql -U postgres -d wins_hub
```

