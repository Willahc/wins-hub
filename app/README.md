# WiNS Hub

FastAPI + Postgres + Alpine.js. Bind-mount source (sem hot-reload — restart de container após edits em Python).

Containers principais:
- `wins_hub_v2-api-1` — serve produção (`winshubcomercial.com.br`)
- `wins_hub-api-1` — runtime backend (compartilha bind-mount `/app` com v2)
- `wins_hub-db-1` — Postgres
- `wins_hub-nginx-1` — reverse proxy + TLS

## Scripts operacionais

### `scripts/bump-cache.sh` — Version bust pra JS estático

Browsers cacheiam arquivos em `/static/js/` por 1 dia (`Cache-Control: max-age=86400`). Sem version bust, edits em `v2-stores.js`/`v2-helpers.js`/`v2-pages.js`/`v2-router.js`/`v2-admin.js` só viram visíveis pros users após `Ctrl+Shift+R` manual.

**Rodar antes de avisar Mari/users após edits em `/static/js/`:**

```bash
./scripts/bump-cache.sh
```

O que faz:
- Atualiza o sufixo `?v=AAAAMMDD_HHMM` em todas as 5 referências `<script>` em `v2/index.html`.
- Idempotente: aborta se rodado 2× no mesmo minuto.
- Não precisa restart de container (HTML v2 servido direto do bind-mount, sem cache nginx).

Depois do bump, **F5 normal** do user já pega o JS novo (browser vê URL diferente).

Cobertura:
- `<script defer>` × 4 (v2-stores, v2-helpers, v2-pages, v2-router) em `<head>`
- `document.write(<script>)` × 1 (v2-admin.js, carregado só em `/admin*`)

### Outros scripts em `scripts/`

Pipelines de captação, enrichment, validação — ver headers individuais.
