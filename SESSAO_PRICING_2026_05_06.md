# Sessão Pricing — WiNS HUB
**Data:** 2026-05-06 · **Escopo executado:** P1 (Mínimo Viável)

## Justificativa do escopo P1

- Pitch sexta 08/05 + lançamento segunda 11/05 → janela apertada
- Mexer em fluxo de desbloqueio em prod = risco alto (já tem cobrança avulsa via MP funcionando)
- Bugs MP ativos amplificam risco de mexer no webhook agora:
  - `project_downgrade_silencioso_planos` (downgrade ao confirmar plano "menor")
  - `project_bug_signup_plano_inicial` (signup atribuindo plano sem pagamento real)
- 4 modalidades não precisam estar 100% no produto pro pitch — basta o MVP do checkout aceitar e o preço bater

## O que foi feito

### B0 — Backups
- DB pré: `/root/backups/wins_hub/wins_hub_pre_pricing_20260506_2249.sql` (1.3 GB)
- main.py pré: `/root/wins_hub/app/main.py.bak_pre_pricing_20260506_2250` (121 KB)
- DB pós: `/root/backups/wins_hub/wins_hub_pos_pricing_20260506_2256.sql` (1.3 GB)

### B2 — Migration aplicada (10 colunas + constraint + índice)
- `prestadores.modalidade VARCHAR(20) DEFAULT 'MENSAL'` + `chk_modalidade` CHECK
- `prestadores.preco_pago_mes DECIMAL(10,2)`
- `prestadores.preco_pago_total DECIMAL(10,2)`
- `prestadores.ciclo_inicio TIMESTAMPTZ DEFAULT NOW()`
- `prestadores.ciclo_fim TIMESTAMPTZ`
- `prestadores.proximo_billing TIMESTAMPTZ`
- `prestadores.auto_renovacao BOOLEAN DEFAULT TRUE`
- `prestadores.periodo_avaliacao_fim TIMESTAMPTZ`
- `prestadores.renunciou_avaliacao BOOLEAN DEFAULT FALSE`
- `prestadores.creditos_liberados_em TIMESTAMPTZ`
- Índice parcial `idx_prestadores_avaliacao_pendente`
- **Adicional**: 2 colunas em `pagamentos` (`modalidade`, `renunciou_avaliacao`) para carregar a intenção até o webhook

### B3 — Constantes Python (`main.py`, após `PLANOS`)
- `PRECOS_MENSALIDADE` — 4 modalidades × 2 planos pagos com `valor_mes_centavos`, `valor_total_centavos`, `duracao_meses`, `desconto_pct`
- `DESBLOQUEIOS_INCLUSOS = {"GRATUITO": 0, "STANDARD": 3, "PREMIUM": 10}`
- `LIMITE_OBRA_INCLUSA_BRL = 10_000_000`
- `preco_avulso_por_valor_obra(valor)` → 97 / 297 / 697 / 997 reais (em centavos)

Mantém `PLANOS` legado para compatibilidade com fluxo existente.

### B5 ajustado — `criar_preferencia` + webhook MP

**`POST /api/pagamento/criar_preferencia`** (sync, psycopg2) agora aceita:
- `modalidade` (MENSAL | TRIMESTRAL | SEMESTRAL | ANUAL, default `"MENSAL"`)
- `renunciar_avaliacao` (bool, default `False`)

Calcula `preco_centavos` via `PRECOS_MENSALIDADE[plano][modalidade].valor_total_centavos`. Valida modalidade (HTTP 400 se inválida). Persiste modalidade + renunciou_avaliacao em `pagamentos`. Metadata MP carrega o mesmo, mais o título inclui `{Plano} {Modalidade.title()} ({duracao_meses} meses)`.

**Webhook MP** (`/api/pagamento/webhook`) ao confirmar `approved` agora:
- Lê modalidade do pagamento
- Calcula ciclo via `make_interval(months => duracao_meses)`
- Atualiza prestador com `plano`, `plano_expira` (até ciclo_fim), `modalidade`, `preco_pago_mes`, `preco_pago_total`, `ciclo_inicio = NOW()`, `ciclo_fim`, `proximo_billing`
- Fallback se modalidade desconhecida → comportamento legado (30 dias)

**Importante**: webhook **NÃO toca** em `periodo_avaliacao_fim`, `renunciou_avaliacao`, `creditos_liberados_em`. Esses campos ficam pra próxima sessão (regra dos 7 dias).

### B8 — Smoke tests

| Teste | Esperado | Resultado |
|---|---|---|
| Home prod | 200 | 200 (143ms) |
| `/api/dashboard/ouro_count` | 288 | 288 ✅ |
| `/api/dashboard/prata_count` | 243 | 243 ✅ |
| `/api/dashboard/pipeline_count` | 744 | 744 ✅ |
| POST `criar_preferencia` sem auth | 401 | 401 ✅ |
| POST `criar_preferencia` modalidade `FOO` (com auth) | 400 + msg | 400 "Modalidade inválida" ✅ |
| POST `criar_preferencia` plano `BAR` (com auth) | 400 + msg | 400 "Plano inválido" ✅ |
| POST `criar_preferencia` STANDARD MENSAL (com auth) | 200 + preco_centavos=19700 | 200, preco_centavos=19700, preference_id retornado ✅ |
| Modalidade persistida em `pagamentos` | tipo=plano, modalidade=MENSAL, renunciou_avaliacao=f | ✅ |
| Constantes `PRECOS_MENSALIDADE` (sanity 8 combos) | bate com tabela | ✅ STANDARD R$197/531/1002/1884; PREMIUM R$497/1341/2532/4764 |
| `preco_avulso_por_valor_obra` (4 faixas) | 97/297/697/997 | ✅ |
| Compile main.py | OK | OK |

API up sem warnings, sem regressão em endpoints existentes.

## O que NÃO foi feito (e por quê)

### B4 — Regra dos 7 dias no `/desbloquear`
**Skipped.** O fluxo atual de `/api/obras/{oid}/desbloquear` (linha 2584) usa lógica complexa por **faixa de valor da obra** + tabela `desbloqueios_plano` (saldo mensal) + cobrança avulsa via MP. Adicionar bloqueio de 7 dias agora requer decisão de produto: bloqueia tudo (incluso + avulso) ou só incluso? Mexer antes do launch é risco vs benefício.

### B6 — Cron de liberação de créditos
**Skipped.** Depende de B4 estar em prod — sem `periodo_avaliacao_fim` sendo populado, cron seria no-op. Faz sentido na sessão que implementar B4.

### B7 — Frontend (página de planos)
**Skipped.** Não existe `/planos.html` no projeto — só `index.html` (com modal de upgrade), `login.html`, `esqueci.html`, `reset.html`, `score.html`. Implementar requer decisão: nova página dedicada ou modal expandido? William edita amanhã (quinta) com tempo.

### Bugs MP existentes
**Documentados, não tocados:**
- `project_downgrade_silencioso_planos` — webhook MP faz downgrade silencioso quando confirma plano de menor hierarquia. Risco amplificado agora que webhook tem mais branches (modalidade).
- `project_bug_signup_plano_inicial` — signup pode atribuir plano ≠ GRATUITO sem pagamento real. Não bloqueante para checkout, mas atenção pra QA do launch.

## Próxima sessão — pendências priorizadas

### Prioridade ALTA (antes do launch ou logo depois)
1. **Decisão de produto** — bloqueio total vs só incluso durante 7 dias?
2. **B7** — frontend dos planos (toggle modalidade + checkbox renúncia + box CDC). Quinta 07/05 manhã.
3. **B4 + B6** — regra dos 7 dias + cron de liberação. Aplicar junto, depois do B7.
4. **Bugs MP** — investigar `downgrade_silencioso` + `signup_plano_inicial` antes do launch.

### Prioridade MÉDIA (pós-launch)
5. Auto-renovação real (today: campo existe mas webhook só processa one-shot — falta loop de cobrança recorrente)
6. Cancelamento com reembolso integral nos 7 dias (interface + integração estorno MP)
7. Fluxo de upgrade/downgrade entre modalidades (proporcionalizar resíduo)

## Frontend — validação visual pendente (William executa)

- [ ] Login STANDARD/PREMIUM → checkout: ainda manda só `plano` (não `modalidade`) — testa fallback (`MENSAL` default) ✅ funciona
- [ ] Após B7 implementado: toggle MENSAL/TRIMESTRAL/SEMESTRAL/ANUAL nos cards
- [ ] Após B4 implementado: cliente novo no plano vê banner "X dias restantes pra liberação"
- [ ] Após B4 implementado: checkbox de renúncia desbloqueia créditos imediatos
