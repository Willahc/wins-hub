# Ferramentas Candidatas — WiNS Hub

> Catálogo de ferramentas open-source úteis ao roadmap, classificadas por urgência.
> Decisão de adoção é deliberada — cada item tem **problema resolvido** e **quando considerar**.
> Versão 1.0 · 2026-05-13

## Tiers

- **🔴 INSTALAR AGORA** — resolvem dor presente, baixo custo de adoção.
- **🟡 INSTALAR DURANTE SPRINT** — necessárias quando um captador específico do roadmap exigir.
- **🟢 CATALOGAR PÓS-LAUNCH** — boas candidatas após estabilizar o produto.
- **❌ DESCARTADAS** — avaliadas e rejeitadas (com motivo).

## Categorias (13)

1. Scraping resiliente
2. Parsing inteligente
3. Detecção e dedup
4. NLP / Extração estruturada
5. Orquestração
6. Monitoramento
7. Decisor enrichment
8. Validação de dados
9. Banco de dados
10. UI / Frontend
11. Geo + Mapping
12. LLM ops
13. Comercial / CRM

---

## 🔴 TIER 1 — INSTALAR AGORA (4 ferramentas)

> Dor presente, ROI imediato, baixo risco. Instalar nesta sessão (PARTE 2).

| # | Nome | Repo | Categoria | Problema resolvido | Quando considerar |
|---|------|------|-----------|-------------------|-------------------|
| 1 | **brutils** | https://github.com/brazilian-utils/brutils-python | 8 Validação | Validação CNPJ/CPF/CEP com DV. Hoje fazemos regex + DV manual em vários pontos (captar_noticias_setoriais.py, decisor_lookup.py). Centraliza e elimina bug-prone reimplementação. | Agora — eliminação de débito técnico imediato. |
| 2 | **pdfplumber** | https://github.com/jsvine/pdfplumber | 2 Parsing | Extração estruturada de PDF (tabelas + texto + posição). DNIT boletim de medição + diários estaduais MG/PR vêm em PDF; hoje parseamos só texto plano (PyPDF2 ou nada). | Agora — habilita captadores do top 10 que tocam PDFs. |
| 3 | **trafilatura** | https://github.com/adbar/trafilatura | 1 Scraping | Extração de conteúdo principal de página HTML (drop nav/footer/ads), de qualidade superior a readability/bs4 manual. Hoje captar_noticias_setoriais.py manda HTML cru pro Haiku — desperdiça tokens. | Agora — corta ~30% do custo Haiku de noticias. |
| 4 | **dateparser** | https://github.com/scrapinghub/dateparser | 4 NLP | Parsing de data em linguagem natural PT-BR ("daqui a 6 meses", "outubro/2027", "Q3 2026", "1º trimestre"). Hoje deixamos Haiku tentar — caro e instável. | Agora — accuracy↑ + custo↓ em campo `prazo_inicio_operacao`. |

---

## 🟡 TIER 2 — INSTALAR DURANTE SPRINT MAPEAMENTO

> Necessárias quando captadores específicos do roadmap exigirem. Não instalar até o gatilho.

### Categoria 1 — Scraping resiliente

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **Playwright (Python)** | https://github.com/microsoft/playwright-python | Páginas com JS pesado (SPA), formulários multi-step, captcha cooperativo. Atualmente flaresolverr cobre Cloudflare, mas não SPA. | Quando primeiro DOE (SP/RJ) ou JUCE exigir interação JS real. Container precisa do `mcr.microsoft.com/playwright/python` base ou `playwright install` + libs sistema. |

### Categoria 11 — Geo + Mapping

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **Nominatim self-hosted** | https://github.com/osm-search/Nominatim | Geocoding de endereço → coordenadas + UF/município. Notícias chegam com cidade no texto mas sem UF estruturado (~7 obras notícia 13/05 ficaram sem UF). | Quando vol > 100 obras/mês precisando de geocoding. Self-host pra evitar quota Google Maps. Alternativa intermediária: API gratuita ViaCEP + IBGE pra inferir UF de cidade. |

### Categoria 4 — NLP / Extração estruturada

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **spaCy pt_core_news_lg** | https://github.com/explosion/spaCy | NER ofline (PT-BR): pessoa, empresa, local, valor monetário. Pré-filtro antes do Haiku → corta tokens em notícias longas. | Quando custo Haiku > R$ 500/mês em extração de notícia. Setup: 600 MB model + 1 GB RAM. Vale só se volume cresce 3x. |

---

## 🟢 TIER 3 — CATALOGAR PÓS-LAUNCH

> Boas candidatas mas precisam estabilidade pra avaliar fit. Não atacar antes do launch.

### Categoria 1 — Scraping resiliente

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **Scrapy** | https://github.com/scrapy/scrapy | Framework completo de crawling com retries, throttling, pipelines. | Quando captadores ultrapassarem 15 e a heterogeneidade ficar ingerenciável. Hoje requests + httpx atendem. |
| **undetected-chromedriver** | https://github.com/ultrafunkamsterdam/undetected-chromedriver | Selenium com fingerprint stealth. | Para fontes hostis que detectam Playwright. Risco ToS. |
| **httpx-socks** | https://github.com/romis2012/httpx-socks | Proxy SOCKS5 em httpx. | Se LinkedIn vagas / fontes com geo-block. |

### Categoria 2 — Parsing inteligente

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **unstructured.io** | https://github.com/Unstructured-IO/unstructured | Parsing universal (PDF/DOCX/HTML/PPTX) com chunking pronto. | Quando documentos heterogêneos virarem regra (RIs, prospectos, atas). |
| **Tesseract OCR** | https://github.com/tesseract-ocr/tesseract | OCR pra PDFs imagem (alguns diários estaduais MG/SC). | Quando >20% dos PDFs alvo forem imagem. Padrão é pdfplumber primeiro, tesseract fallback. |

### Categoria 3 — Detecção e dedup

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **datasketch (MinHash LSH)** | https://github.com/ekzhu/datasketch | Dedup de notícias similares em escala (mesma obra reportada em 5 portais). | Quando vol > 1000 notícias/mês. Hoje hash exato + filtro de URL atende. |
| **rapidfuzz** | https://github.com/rapidfuzz/RapidFuzz | Fuzzy matching rápido C++. Útil pra normalizar nome de empresa ("EMS Pharma" = "EMS S.A."). | Em dedup de empresa + match nome obra. |
| **re2** | https://github.com/google/re2 | Regex em tempo linear (evita catastrophic backtracking). | Quando regex de filtro virar gargalo (já não é hoje). |

### Categoria 4 — NLP / Extração estruturada

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **spaCy (geral)** | https://github.com/explosion/spaCy | NER + parser. Já listado em tier 🟡 com modelo PT específico. | Modelo `_lg` no tier 🟡; `_sm` pode entrar antes se for só identificar entidades. |
| **outlines** | https://github.com/dottxt-ai/outlines | Generation estruturada (JSON schema) com modelos abertos. | Se sair de Haiku pra modelos self-host (custo). |
| **DSPy** | https://github.com/stanfordnlp/dspy | Otimização programática de prompts. | Para prompts longos do Haiku (filtro decisor C5, extração notícia). Ganho 10-30% accuracy + cost. |

### Categoria 5 — Orquestração

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **Celery + Redis** | https://github.com/celery/celery | Background jobs distribuídos com retry, priority, dead-letter. | Quando jobs ultrapassarem 5 simultâneos e precisarmos isolar (decisor lookup + enrichment + alerta realtime concorrem hoje na mesma API). |
| **APScheduler** | https://github.com/agronholm/apscheduler | Scheduler dentro do processo Python (alternativa a crontab). | Para dev/staging sem cron do host. Em prod, crontab é mais simples. |

### Categoria 6 — Monitoramento

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **Sentry** | https://github.com/getsentry/self-hosted | Error tracking + performance. Hoje só temos logs em arquivo. | Quando launch e churn começar — bug em 0.5% dos usuários invisível hoje. |
| **Prometheus + Grafana** | https://github.com/prometheus/prometheus + https://github.com/grafana/grafana | Métricas histórica (latência, requests/min, custo Haiku). | Em paralelo ao Sentry. Já temos Metabase pra dashboards de produto. |
| **healthchecks.io (self-hosted)** | https://github.com/healthchecks/healthchecks | Dead man's switch pra crons. Alerta se orchestrator não rodar. | Logo após launch — hoje cron failing silenciosamente é risco real. |

### Categoria 7 — Decisor enrichment

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **emailrep.io** | https://emailrep.io/ | Reputação de email (catch-all, score). Complementa SMTP verify atual. | Quando taxa de bounce do outreach > 5%. |
| **SpiderFoot** | https://github.com/smicallef/spiderfoot | Footprinting OSINT (domínios, subdomínios, leaks). | Já temos recon_intel próprio. Avaliar substituição só se ele estagnar. |

### Categoria 8 — Validação de dados

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **brasilapi-py** | https://github.com/brasilapi/brasilapi-python | Wrapper Python pra BrasilAPI (já consumimos via httpx direto). | Sem urgência — hoje wrapper próprio funciona. |

### Categoria 9 — Banco de dados

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **TimescaleDB** | https://github.com/timescale/timescaledb | Hypertables pra séries temporais (log_captacao, interacoes). | Quando log_captacao passar de 10 M rows. Hoje 191. |
| **pg_partman** | https://github.com/pgpartman/pg_partman | Particionamento declarativo. | Antes de matches_obra_prestador passar de 50 M rows. Hoje 3.44 M. |
| **MeiliSearch** | https://github.com/meilisearch/meilisearch | Busca full-text typo-tolerant pra obras + fornecedores. | Quando dashboard de busca crescer (hoje filtros estruturados atendem). |

### Categoria 10 — UI / Frontend

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| _(reservado)_ | — | Avaliar se Alpine continua suficiente quando dashboard escalar. | Pós-launch, ver §❌ DESCARTADAS. |

### Categoria 11 — Geo + Mapping

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **shapely + geopandas** | https://github.com/shapely/shapely + https://github.com/geopandas/geopandas | Operações geométricas (raio X km de obra, intersecção com bacia hidrográfica, etc). | Quando feature "fornecedores num raio de N km da obra" entrar no roadmap. |
| **H3 (Uber)** | https://github.com/uber/h3-py | Indexação hexagonal pra busca espacial rápida. | Em conjunto com shapely, se busca espacial virar hot path. |

### Categoria 12 — LLM ops

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **LiteLLM proxy** | https://github.com/BerriAI/litellm | Abstração multi-provider (Anthropic + OpenAI + local) com fallback + budget. | Quando custo Anthropic > R$ 2k/mês ou precisarmos fallback em outages. |

### Categoria 13 — Comercial / CRM

| Nome | Repo | Problema resolvido | Quando considerar |
|------|------|-------------------|-------------------|
| **EspoCRM** | https://github.com/espocrm/espocrm | CRM self-host pra time de vendas. | Quando equipe comercial > 3 pessoas. Hoje 1 cliente piloto, CRM in-app atende. |
| **Cal.com** | https://github.com/calcom/cal.com | Agendamento de calls com clientes pilot. | Se ciclo de discovery virar gargalo. |
| **Plausible Analytics** | https://github.com/plausible/analytics | Analytics web GDPR-friendly. | Após launch para tracking de funnel. Hoje sem analytics. |

---

## ❌ DESCARTADAS

| Nome | Motivo |
|------|--------|
| **Maltego CE** | Overkill pra footprinting de empresa B2B. SpiderFoot + recon próprio cobrem 95% do uso. |
| **HTMX** | Alpine.js já cobre interatividade da SPA atual. HTMX exigiria refactor de rotas pra server-side rendering — custo de migração não compensa. |
| **Pico.css / Bulma** | Stack atual usa CSS vanilla + utilitários customizados. Tailwind seria a primeira opção se mudássemos, não micro-frameworks. |

---

## Sumário

| Tier | Qtd | Custo de adoção | Quando |
|------|-----|-----------------|--------|
| 🔴 INSTALAR AGORA | 4 | ~30 min total | Esta sessão |
| 🟡 SPRINT | 3 | 1-3 dias cada | Gatilho específico durante sprint mapeamento |
| 🟢 PÓS-LAUNCH | 28 | 1-5 dias cada | Após estabilizar produto |
| ❌ DESCARTADAS | 3 | — | — |
| **TOTAL** | **38** | | |

## Como decidir adoção (tiers 🟡/🟢)

Antes de instalar qualquer tier 🟡 ou 🟢:

1. **Gatilho objetivo:** definir métrica que justifica (ex: "Hunter quota esgotou 3 meses seguidos" → consider LiteLLM).
2. **Custo total de propriedade:** runtime + memória + dependências transitivas + risco de bug introduzido.
3. **Alternativa interna:** existe forma de resolver com código próprio em < 1 dia? Se sim, fazer próprio.
4. **Documentar em INFRAESTRUTURA.md** (auto-tracking forçará).

Filosofia: **resistir ao impulso de adicionar dependência**. Cada item neste catálogo tem "Quando considerar" justamente pra freá-lo até haver gatilho real.
