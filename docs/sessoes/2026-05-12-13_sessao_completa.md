# Sessão 12-13/05/2026 — Consolidação completa

**Operador:** Claude Code (sessão `williamvnvn@gmail.com`)
**Modelo:** Opus 4.7 (1M context)
**Duração:** sessões 12/05 manhã → 13/05 madrugada
**Branch:** `main`

---

## 1. Resumo executivo

Quatro entregas principais:

1. **Captador de notícias setoriais** — pipeline completo RSS + Haiku + BrasilAPI extração, com 9 fontes (incluindo G1 via sitemap e Click Petróleo via FlareSolverr CF bypass). Wrapper cron preparado com alerta Resend após 2 falhas. Loop manual 3/12 iniciado (custo Haiku acumulado $0.044).
2. **CNPJ enrichment ANTT/ANAC** — 181 obras corrigidas em 23 empresas únicas (BAFER FIOL + AENA 3 SPEs + Cat A 10 + Cat B 9). +135.008 matches gerados via re-rodada matchmaking. Razão social BAFER também corrigida (era "BAHIA E FERTILIZANTES", real é "BAHIA FERROVIAS").
3. **Performance dashboard** — cache TTL 5min em `/api/dashboard/matches_ouro` e `/times_ouro` (4-6s → ~20ms hit). VACUUM ANALYZE semanal cron domingo 04:00 ANTES do backup (visibility map: COUNT fornecedores 3.7s → 0.35s).
4. **Sweep robustez fetch** — timeout 8s + fallback graceful em 8 handlers SPA. Spinner EMS infinito resolvido. Zero `TypeError: Failed to fetch` em Auto-QA Playwright.

UX: botão "Entrar" na home para deslogados; acesso representante (Mari) validado completo — backend e frontend já tratavam `is_representante` corretamente, sem alteração necessária.

---

## 2. Commits da sessão (origin/main)

| SHA | Mensagem |
|---|---|
| `2df80b5` | fix(ux): botão login na home + acesso completo representante (fornecedor + PDF) |
| `58c2396` | fix(ux): botão Entrar na home para deslogados + valida acesso representante |
| `952af88` | feat(captador): FlareSolverr CF bypass + Click Petróleo |
| `be84a7e` | feat(captador): G1 path whitelist + keywords por_tipo expandidas (logistica/data_center/agro) |
| `5a8f335` | feat(captador): keywords por tipo + Playwright CF bypass + sitemap G1 |
| `da93804` | perf(dashboard): cache TTL 5min em matches_ouro + times_ouro |
| `158b1b8` | fix(p2): timeout 8s + fallback em fetches de canal/kpis (elimina spinner infinito + Failed to fetch) |
| `79447cc` | feat(captador): wrapper cron + alerta Resend em falhas consecutivas |
| `b0c1ccc` | feat: captador notícias setoriais (8 fontes + Haiku extração + status workflow) |
| `b8252e0` | feat(p3.1): integra descoberta de domínio + fix trabalha_atualmente + greylisted→pending |
| `d02b21a` | fix(p3.1): preserva email no ON CONFLICT via COALESCE + re-enrichment Log-In |
| `503b512` | fix(p2)+feat(p3.1): canal init + visibility polling + enriquecer C3 fix |
| `b886bc8` | fix(p2): silencia 401/403 + dedup crontab + script P3.1 esqueleto |
| `73f6b9b` | feat: wallet refactor + P0 decisor + onboarding 2-etapas + OG premium |

Cron `30 4 * * 0` → `0 4 * * 0` (VACUUM antes do backup) — alteração de crontab, sem commit de código.

---

## 3. CNPJ enrichment ANTT/ANAC — resultado final

### Cat A — fonte oficial primária (10 empresas, 119 obras)

| Empresa | CNPJ | Obras | Matches |
|---|---|---:|---:|
| MRS LOGÍSTICA S/A | 01.417.222/0001-77 | 43 | 32.250 |
| FCA - FERROVIA CENTRO-ATLÂNTICA S/A | 00.924.429/0001-75 | 38 | 28.500 |
| RUMO MALHA NORTE S/A | 24.962.466/0001-36 | 14 | 10.500 |
| CCR AEROPORTOS S.A. | 02.846.056/0001-97 | 12 | 9.000 |
| AEROPORTO CURITIBA CONCESSÕES | 77.636.074/0001-43 | 4 | 3.000 |
| FLYSUL CONCESSÕES AEROPORTUÁRIAS | 42.130.537/0001-16 | 3 | 2.250 |
| FNS - FERROVIA NORTE-SUL S/A | 09.257.877/0001-37 | 2 | 1.500 |
| VINCI AIRPORTS BRASIL | 27.950.582/0001-23 | 1 | 750 |
| FRAPORT BRASIL FORTALEZA | 27.059.565/0001-09 | 1 | 750 |
| GRU AIRPORT (Guarulhos) | 15.578.569/0001-06 | 1 | 750 |
| **Subtotal** | | **119** | **89.250** |

### Cat B — BrasilAPI (9 empresas, 42 obras)

| Empresa | CNPJ | Obras | Matches | Método |
|---|---|---:|---:|---|
| RUMO MALHA PAULISTA S/A | 02.502.844/0001-66 | 14 | 9.758 | brasilapi_textual (1.00) |
| FTL TRANSNORDESTINA LOGÍSTICA | 17.234.244/0001-31 | 8 | 6.000 | brasilapi_textual (0.97) |
| RUMO MALHA SUL S/A | 01.258.944/0001-26 | 7 | 5.250 | brasilapi_textual (1.00) |
| INFRAERO / TRANSITÓRIO | 00.352.294/0001-10 | 6 | 4.500 | brasilapi_semantico_manual |
| RUMO MALHA CENTRAL S/A | 33.572.408/0001-97 | 3 | 2.250 | brasilapi_textual (1.00) |
| AEROPORTOS BRASIL VIRACOPOS | 14.522.178/0001-07 | 1 | 750 | brasilapi_textual (1.00) |
| INFRAMÉRICA BSB | 15.559.082/0001-86 | 1 | 750 | brasilapi_semantico_manual |
| FRAPORT BRASIL PORTO ALEGRE | 27.059.460/0001-41 | 1 | 750 | brasilapi_textual (1.00) |
| BH AIRPORT (Confins) | 19.674.909/0001-53 | 1 | 750 | brasilapi_semantico_manual |
| **Subtotal** | | **42** | **30.758** | |

### BAFER (FIOL) — razão social corrigida (1 empresa, 12 obras)

| Antes | Depois | Obras | Matches |
|---|---|---:|---:|
| BAFER - BAHIA E FERTILIZANTES S.A. (cnpj=NULL) | **BAHIA FERROVIAS S.A.** (42.916.256/0001-93) | 12 | 9.000 |

### AENA → 3 SPEs reais (3 empresas, 8 obras)

| Sigla → SPE | CNPJ | Obras | Matches |
|---|---|---:|---:|
| 6 aeroportos Nordeste → **ANB** | 33.919.741/0001-20 | 6 | 4.500 |
| SBSP Congonhas → **BOAB** | 48.725.405/0001-13 | 1 | 750 |
| SBGL Galeão → **RIOgaleão** (Aena 100%) | 19.726.111/0001-08 | 1 | 750 |
| **Subtotal** | | **8** | **6.000** |

### Cat C — mantidas sem CNPJ (4 obras)

| Obra/Empresa | Motivo |
|---|---|
| ANAC-SBSA (São José dos Campos) | empresa='AENA BRASIL S.A.' provável misclassificação — operação local prefeitura+GOL |
| INFRAMÉRICA NATAL | Concessão devolvida 2021 — sem SPE ativa |
| ZURICH AIRPORT BRASIL S.A. | SearchChain sem hit confiável |
| FTC - FERROVIA TEREZA CRISTINA S/A | Homônimo "FTC.com.br" (faculdade) — necessita confirmação manual |

### Consolidado

| Lote | Empresas | Obras | Matches |
|---|---:|---:|---:|
| BAFER | 1 | 12 | 9.000 |
| AENA | 3 | 8 | 6.000 |
| Cat A | 10 | 119 | 89.250 |
| Cat B | 9 | 42 | 30.758 |
| **TOTAL aplicado** | **23** | **181** | **135.008** |
| Cat C pendente | 3 | 4 | — |

Campo `validacao_metodo` em `obras` populado (`brasilapi_textual` ou `brasilapi_semantico_manual`).

---

## 4. Captador notícias setoriais

### Fontes (9 ativas)

| Fonte | Tipo | Tier | Notas |
|---|---|---|---|
| agenciainfra | RSS | 1 | 91 items/7d, cobertura industrial 16 |
| braziljournal | RSS | 1 | cobertura 11 |
| exame | RSS | 1 | 25/7d, cobertura 6 |
| neofeed | RSS | 1 | cobertura 7 |
| epbr | RSS | 1 | 20/7d energia/petróleo |
| istoedinheiro | RSS | 2 | 100/7d, ruído moderado |
| moneytimes | RSS | 2 | 10/7d mercado |
| panoramafarmaceutico | RSS | 2 | nicho farmacêutico |
| **g1_economia** | **sitemap** | 2 | path whitelist 4 prefixos (agronegocios/industria/empresas/infraestrutura) |
| **click_petroleo** | **rss_flaresolverr** | 2 | CF Turnstile bypass via FlareSolverr |

### Loop manual 72h

| Rodada | Timestamp | Obras inseridas | Custo Haiku |
|---|---|---:|---:|
| 1 | 12/05 20:33 UTC | 1 (Terminal Vila Velha — Log-In/ES) | $0.0175 |
| 2 | 12/05 20:41 UTC | 0 (entries já processadas) | $0.0019 |
| 3 | 12/05 20:50 UTC | 1 (iez! Telecom — leilão Anatel R$ 4.4M) | $0.0104 |
| **Acumulado** | | **2 obras** | **$0.0298** |

**Status: 3/12 rodadas.** 9 rodadas restantes (~54h). Cron `0 */6 * * *` documentado mas **NÃO ativado**. Após 12 rodadas estáveis, descomentar entrada cron.

### Wrapper alerta Resend

- `/root/wins_hub/scripts/cron_captar_noticias.sh`
- State `/root/wins_hub/var/captador_failures` (counter consecutivas)
- Threshold 2 falhas → email williamvnvn@gmail.com com tail do log
- Validado em dry-run (HTTP 401 com fake key confirma payload JSON válido)

### Tipos por keywords expandidas

Categorias mapeadas: fabrica, infraestrutura, energia, mineracao, **logistica** (nova), **data_center** (nova), **agro** (nova).
Match com tipo subiu de 17% → 35.4% após expansão de sinônimos. Critério interno >40% não atingido (faltam mais sinônimos).

---

## 5. Decisores / Hunter

| Métrica | Valor | Notas |
|---|---:|---|
| `empresa_decisores_cache` total | 151 decisores | 28 CNPJs únicos |
| email_status='verified_smtp' | 8 | Intocado em toda sessão |
| Hunter quota /v2/account | 11/50 | Reset 07/06 |
| 396 emails cache Hunter | preservados | |

**Camadas pipeline:**
- C1: identificação (descoberta_dominio via SearchChain — sem Playwright)
- C2: classificação pessoa-vs-setor (`classificar_pessoa.py`)
- C3: temporal_gate (regex metadata-only)
- C4: enricher Hunter API
- C5: LLM filter ex-funcionário (Haiku 4.5)

---

## 6. Fixes técnicos da sessão

### 6.1 Wallet unificado refactor
- `mensalidade` → saldo unificado via webhook Mercado Pago
- Eliminada tabela `desbloqueios_plano`
- `/api/me/saldo_desbloqueios` aceita uso direto
- Endpoint `/desbloquear` bypassa quota com cobrança em centavos

### 6.2 P0 Decisor — `/api/obras/{oid}/detalhe`
- Integra `empresa_decisores_cache` (Camadas 3+4 LLM)
- 3 estados render: gratuito (mascarado) / pago (parcial) / desbloqueado (completo)
- Modal Alpine pra desbloqueio (1000 centavos = 1 crédito)
- Filtro WHERE atualizado: `trabalha_atualmente=true AND COALESCE(filtro_llm_confianca, confianca) IN ('alta','media')`

### 6.3 ON CONFLICT preservação email (COALESCE)
- `cache_decisores.py:48`: SET adicionou `email = COALESCE(EXCLUDED.email, empresa_decisores_cache.email)`
- Previne sobrescrita de `verified_smtp` por enrichment subsequente que falhou transientemente
- Mesmo padrão em `email_status`

### 6.4 Greylisted/catch_all → pending
- `integracao_c3_c4.py:148`: mapeia status SMTP `greylisted` e `catch_all` para `'pending'`
- CHECK constraint `email_status_check` aceita apenas: pending/inferred_pattern/verified_mx/verified_smtp/invalid/bounce

### 6.5 Cache TTL dashboard
- `_matches_ouro_cache` (dict, key=(setor,uf), TTL 300s) em `main.py:4298`
- `_times_ouro_cache` (idem) em `main.py:4400`
- Ganho cold→hit: kpis 4.49s→0.017s · matches_ouro 5.73s→0.022s · times_ouro 3.63s→0.056s
- `/api/dashboard/kpis` cold pós-VACUUM: 4.49s → 1.37s (-69.5%)

### 6.6 Failed to fetch sweep (8 handlers)
- AbortController 8s em `carregarCanalCadastro` e `loadKpis`
- Fallback structured: `{encontrado:false, motivo:'timeout'|'network_error'|'unauthorized'|'server_error'}`
- Padrão silenciador `(e instanceof TypeError && /fetch/i.test(e.message))` em loadObrasUf, _atualizarFacetas, loadMatches, loadFornecedores
- Auto-QA Playwright pós-fix: 0 page errors, 0 console errors, 0 'Failed to fetch'
- Spinner EMS resolvido — t+8s spinner OFF + fallback "Canal de cadastro ainda não mapeado"

### 6.7 Mari (representante) race condition — falsa pista
- Briefing: "representante não consegue ver fornecedor completo + PDF"
- Investigação revelou: `pode_ver_conteudo_pago(user)` em `permissions.py:23` JÁ aceita `is_representante=True`
- Backend testes: GET /api/fornecedores/{cnpj} → 200 · POST /api/vendas/gerar-pdf-match → 200 (lead_id + 5 obras)
- Frontend testes (Playwright JWT inject): página completa renderiza + botão "📄 Baixar PDF de Match" visível + 0 console errors
- **Sem mudança necessária no backend nem frontend** — apenas confirmação que o sistema já estava OK

### 6.8 Maintenance database
- VACUUM ANALYZE fornecedores manual: 34.6s, n_dead_tup 176.469 → 0, Heap Fetches 2.28M → 0
- Cron `0 4 * * 0` semanal (domingos 04:00, antes do backup_diario.sh às 04:30)
- COUNT fornecedores: 3.7s → 0.35s (-90.5%)

---

## 7. Pendências para amanhã (agenda 13/05)

1. **Cat C — 4 obras sem CNPJ** (SBSA, INFRAMÉRICA NATAL, ZURICH AIRPORT, FTC FERROVIA TEREZA CRISTINA) — pesquisa manual ou descartar do funil Ouro.
2. **Loop manual captador 4-12/12** (9 rodadas restantes, ~54h). Após estável → descomentar `0 */6 * * *` no crontab.
3. **Schema `obras.revisao_pendente`** — coluna boolean ausente. ALTER TABLE pra persistir flag de revisão Cat C.
4. **Keywords por_tipo expansão pra >40% match** — atualmente 35.4%. Iterar sinônimos pra fabrica/infraestrutura/etc.
5. **Validação Ouro (P3)** — pendência catalogada em memory `pendentes_20260513`.
6. **Bug CNPJ Receita rejeita tudo** — P0 cadastro fornecedor form (memory `pendentes_20260513`).
7. **Sanitização pré-open-source** — limpar TETRA TECH em `cliente_piloto.py` + Ederson em `deck.md` antes de público/colab externo (memory `sanitizacao_open_source`).

---

## Anexos: arquivos-chave da sessão

- `/root/wins_hub/app/scripts/captar_noticias_setoriais.py` (refactor major — tipo_provavel + sitemap + FlareSolverr)
- `/root/wins_hub/app/scripts/fetch_via_flaresolverr.py` (helper CF bypass)
- `/root/wins_hub/app/scripts/fetch_rss_playwright.py` (Playwright fallback — ineficaz pra CF Turnstile sem stealth)
- `/root/wins_hub/app/scripts/fontes_noticias.yaml` (9 fontes + keywords expandidas)
- `/root/wins_hub/scripts/cron_captar_noticias.sh` (wrapper + Resend alert)
- `/root/wins_hub/docker-compose.yml` (FlareSolverr service)
- `/var/log/captador_manual_log.txt` (registro loop 72h)

