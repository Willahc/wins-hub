# Mapa de Viabilidade — Sprint de Enriquecimento CNAE 20–33
**Data:** 22/06/2026 · **Base:** 1.625 obras OURO com CAPEX confirmado (R$ 3.162 bi) · **Sem alteração em prod — só números**

## 1. Demanda da cadeia (Matriz Leontief) × inventário RFB

| Div CNAE | Setor | Demanda R$bi | Ativos na base | Com e-mail | Com decisor | Diagnóstico |
|---|---|---:|---:|---:|---:|---|
| **26** | Eletrônicos/informática | **273,7** | 2.681 | 2.228 | 2 | 🟢 enriquecer |
| **20** | Químicos | **91,3** | 422 | 330 | 0 | 🟢 enriquecer |
| **29** | Autopeças/veículos | **86,0** | 209 | 167 | 9 | 🟢 enriquecer |
| **23** | Cimento/min. não-met. | **77,0** | **0** | 0 | 0 | 🔴 adquirir (sem inventário) |
| **24** | Siderurgia/aço | **72,8** | **0** | 0 | 0 | 🔴 adquirir |
| 25 | Produtos de metal | 55,4 | 112.492 | 101.486 | 1 | 🟡 já coberto (Comercial) |
| 33 | Manutenção/instalação | 53,9 | 49.342 | 45.835 | 1 | 🟡 já coberto |
| **22** | Borracha/plástico | **39,9** | **0** | 0 | 0 | 🔴 adquirir |
| **27** | Equip. elétricos | 38,7 | 2.464 | 2.153 | 0 | 🟢 enriquecer |
| **28** | Máquinas mecânicas | 35,8 | 6.236 | 5.011 | 1 | 🟢 enriquecer |
| 19 | Refino | 175,9 | 858 | 629 | 31 | 🟡 concentrado (Petrobras) |
| 17 | Celulose/papel | 132,0 | 163 | 123 | 8 | 🟡 concentrado |

> Top-10 demanda **geral** (inclui setor-próprio das obras): 06 Petróleo R$1.102bi · 35 Energia R$885bi · 41 Construção R$698bi · 52 Armazenagem R$360bi · 26 Eletrônicos R$274bi · 45 Comércio R$221bi · 07 Minério R$179bi · 19 Refino R$176bi · 36 Saneamento R$155bi · 49 Transporte R$149bi. (06/07/45 ≈ 0 fornecedores: extração/comércio fora da base.)

## 2. O gargalo NÃO é e-mail — é decisor
- **E-mail já existe** em ~88–93% dos ativos (e-mail cadastral RFB) — mas é genérico (contador/sócio), não decisor de compras.
- **Decisor nomeado é ~0** em todas as divisões industriais (CRM cobre só ~2,6k empresas). É isto que web_search/Hunter produzem.
- 3 setores de alta demanda têm **inventário ZERO** (cimento R$77bi, aço R$73bi, plástico R$40bi = **R$190bi de demanda sem 1 fornecedor**). Enriquecimento não resolve — precisa **aquisição RFB** (esses CNPJs existem na RFB completa, não foram importados).

## 3. Esforço de enriquecimento (decisor nomeado + e-mail)
**Quota Hunter HOJE = 0** (2000/2000 usados, reset **11/07/2026** → 2.000/mês). Throughput de descoberta (alinhado aos crons atuais ~500 leads/dia):

| Recurso | Capacidade | Custo | Saída |
|---|---|---|---|
| web_search free-first (Serper) | ~300–400 empresas/dia | grátis | nome+cargo do decisor (e-mail **não verificado** → PRATA) |
| Hunter batch | **0/dia até 11/07**, depois ~2.000/mês (~65/dia) | quota | e-mail **verificado** → OURO (yield ~44%) |

**Tradução:** até 11/07 dá pra montar o *inventário de nomes* (PRATA) via web_search; e-mail verificado (OURO) só a partir de 11/07, a ~**725 decisores/mês** (2.000 Hunter × 44%).

## 4. Plano faseado proposto

### 🟢 Fase 1 — top 3 demandados COM inventário (26, 20, 29)
- Universo: **3.312 empresas** (~2.900 já com e-mail genérico) · demanda combinada **R$ 451 bi**.
- web_search free-first: varre tudo em **~9 dias** → ~1.650 decisores-nome (PRATA), **começa já** (quota 0 não trava).
- Hunter (pós-11/07): verifica os ~1.650 → ~**725 decisores OURO em ~1 mês** (consome ~1 cota mensal).
- **Maior impacto/esforço**: 26 sozinho = R$274bi com só 2.681 empresas (densidade de demanda altíssima).

### 🟡 Fase 2 — manufatura restante com inventário (27, 28, 17, 19 primeiro; 25, 33 sob demanda)
- 27+28+17+19 = ~9.700 empresas (~3 semanas web_search + 2ª cota Hunter).
- 25 (112k) e 33 (49k) NÃO varrer em massa — só enriquecer os que casarem com obras específicas (densidade de demanda baixa por empresa).

### 🔴 Trilha de Aquisição (paralela) — 23, 24, 22 (R$190bi, inventário 0)
- Importar da RFB completa os CNPJs de cimento (23), aço (24) e plástico/borracha (22) — passo de **dados** (RFB pública, barato), não de descoberta.
- Depois entram no mesmo funil da Fase 1.

## 5. Números-resumo para decisão
- **Demanda industrial endereçável (20–33):** ~R$ 900 bi distribuída em 10 divisões.
- **Inventário enriquecível hoje (Fase 1+2 com decisor a achar):** ~13.000 empresas (26,20,29,27,28,17,19).
- **Inventário a adquirir (23,24,22):** 0 hoje → milhares disponíveis na RFB.
- **Velocidade de conversão a OURO:** 0 até 11/07; ~725/mês depois (limite Hunter). web_search gera PRATA antes disso.

*Sem nenhuma escrita em produção. Próximo passo sugerido: aprovar Fase 1 (web_search nos 3.312 da div 26/20/29) — produz leads PRATA imediatamente, sem depender da quota Hunter.*
