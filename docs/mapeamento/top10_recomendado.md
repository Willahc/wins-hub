# Top 10 fontes recomendadas pré-launch

> Pipeline tático: o que atacar nas próximas 6 semanas pra dobrar cobertura antes do launch.
> Versão 1.0 · 2026-05-13

## Critérios de seleção

Selecionado por (em ordem):

1. **Pega CAPEX que hoje passa batido** — fontes que cobrem tipos não tocados (SANEAMENTO, SAÚDE, IMOBILIÁRIO INDUSTRIAL) ou antecipação ausente (consultas públicas, planos decenais).
2. **Esforço razoável** — entregável em 5-10 dias por fonte, sem dependência externa.
3. **Custo operacional próximo de zero** — preferimos API/RSS oficial a scraping com proxy/Hunter.
4. **Sinal qualificado** — fonte oficial > setorial > informal pra evitar ruído.

## Ranking

### 1. `compras_gov` — API oficial Compras.gov.br

- **Por quê:** Consulta pública + manifestação de interesse precedem o edital formal em 4-26 semanas. Cobre obras federais + estaduais + municipais que aderiram. API REST oficial com swagger.
- **Cobertura nova:** ~80 obras/mês com decisor governamental nominado (ordenador de despesa, comissão de licitação).
- **Esforço:** 4-5 dias engenharia (filtro CNAE + valor + órgão).
- **Risco:** Volume alto exige filtragem agressiva — Haiku precisa cortar não-obra (compra de bens, serviços contínuos).

### 2. `diarios_oficiais_municipais` via querido-diario (top 50 cidades)

- **Por quê:** OKBR (querido-diario) já tem dataset diário público das ~60% capitais. Alvará de obra industrial e mudança ZEIS são sinais de altíssima qualidade. Custo zero de scraping individual.
- **Cobertura nova:** ~200 obras/mês (zoneamento + alvarás + ZEIS).
- **Esforço:** 3-4 dias (consumir dataset + filtros + classificador).
- **Risco:** Dependência do OKBR (mas dataset gratuito e estável).

### 3. `dou` — Diário Oficial da União Seções 1+3

- **Por quê:** Sinal mais autoritativo do executivo federal. Decretos de investimento + avisos de licitação. API JSON oficial in.gov.br. Parser pronto na comunidade.
- **Cobertura nova:** ~50 obras/mês (decretos + editais federais).
- **Esforço:** 5-6 dias (filtros keyword + diff diário + dedup com BNDES/IBAMA já capturados).
- **Risco:** Volume bruto enorme; sem keywords boas, vira ruído.

### 4. `diarios_oficiais_estaduais_top5` (SP/RJ/MG/PR/SC)

- **Por quê:** Concentram ~70% do CAPEX nacional. SP/RJ têm portais HTML; MG/PR/SC têm PDFs estruturados. Licenças ambientais estaduais (LP/LI) precedem IBAMA federal.
- **Cobertura nova:** ~150 obras/mês.
- **Esforço:** 6-8 dias (5 captadores; alguns reutilizam querido-diario, outros próprios).
- **Risco:** PDFs de MG/SC podem precisar OCR (~20%).

### 5. `petrobras_webcast` + plano estratégico anual

- **Por quê:** Petrobras sozinha = R$ 100 B+/ano CAPEX. Plano Estratégico anual + webcast trimestral detalham CAPEX por planta. Maior fonte individual do Brasil.
- **Cobertura nova:** ~5 obras/mês mas com **ticket médio R$ 500 M+**.
- **Esforço:** 5-7 dias (PDF parser + transcript de áudio com Whisper + Haiku para extrair CAPEX por planta).
- **Risco:** Áudio requer transcrição (custo Whisper baixo, ~R$ 0.50/call). Parser PDF pode falhar em apresentações slide-pesadas.

### 6. `cias_saneamento_top6` (SABESP, Iguasa-ex-CEDAE, COPASA, SANEPAR, EMBASA, SANEAGO)

- **Por quê:** Pós-Marco Saneamento 2020 + privatização SABESP 2024 = explosão de PPPs. Setor SANEAMENTO **não tem coverage** hoje. Top 6 cobrem ~80% do mercado.
- **Cobertura nova:** ~25 obras/mês, ticket R$ 50-500 M.
- **Esforço:** 5-7 dias (6 captadores HTML simples + dedup).
- **Risco:** Heterogeneidade entre portais — alguns têm RI pesado, outros licitação pesado.

### 7. `aneel_leiloes_pre_resultado` (calendário + lista de proponentes)

- **Por quê:** Hoje cobrimos vencedores (via SIGA). Falta o **pré-leilão**: lista de empresas habilitadas. Cada habilitado é potencial cliente de EPC subestação/cabeamento.
- **Cobertura nova:** ~5 obras/mês, mas multiplica decisores por 4-8 proponentes/leilão.
- **Esforço:** 3-4 dias (HTML scraper + 6 leilões/ano).
- **Risco:** Baixo — formato consistente desde 2010.

### 8. `dnit` — obras rodoviárias federais

- **Por quê:** PNL 2030 + ordens de início de serviço. Fornecedores de pavimento, britagem, transformadores precisam disso. Não está em nenhuma fonte atual.
- **Cobertura nova:** ~30 obras/mês.
- **Esforço:** 6-8 dias (HTML + PDFs anexos + parser de boletim de medição).
- **Risco:** Medições mensais vêm em PDF imagem ~30% das vezes.

### 9. `secretarias_estaduais_desenvolvimento` (Top 7: SP/MG/RJ/PR/BA/PE/RS)

- **Por quê:** InvestSP/Invest Minas anunciam IDE **antes** do anúncio formal da empresa. Sinal de qualidade comparável a CVM, com 4-12 semanas de antecipação.
- **Cobertura nova:** ~40 obras/mês, foco em fábrica nova.
- **Esforço:** 5-7 dias (7 sites, alguns têm RSS, outros HTML).
- **Risco:** Baixo — instituições governamentais sem anti-bot.

### 10. `eletrobras_ri` + `apex_brasil`

- **Por quê:** Dois quick wins de baixo volume mas alto sinal. Eletrobras pós-privatização vira competidora pública ativa. APEX divulga MoUs e cartas de intenção de IDE.
- **Cobertura nova:** ~15 obras/mês combinadas (3 Eletrobras + 12 APEX).
- **Esforço:** 2-3 dias os dois juntos.
- **Risco:** Mínimo.

## Estimativa total

| Item                         | Valor                          |
| ---------------------------- | ------------------------------ |
| Esforço engenharia           | 45-60 dias-dev                 |
| Custo operacional incremental| R$ 100-500/mês (Haiku adicional) |
| Custo de infra adicional     | R$ 0 (mesma DB + workers)      |
| Cobertura nova (obras/mês)   | ~600 obras                     |
| % aumento sobre base atual   | ~3x na entrada Pipeline        |
| Antecipação média gerada     | +6 semanas (vs base atual)     |
| Setores novos cobertos       | SANEAMENTO, SAÚDE parcial      |
| Fontes federais novas        | 4                              |
| Fontes estaduais novas       | 7 secretarias + 5 diários      |
| Fontes municipais novas      | ~30 (via querido-diario)       |

## Sequenciamento sugerido (sprint 6 semanas)

| Semana | Foco                                                                          |
| -----: | ----------------------------------------------------------------------------- |
| 1      | #1 compras_gov + #10 eletrobras + apex (warm-up, ROI rápido)                  |
| 2      | #3 dou + #2 querido-diario municipais                                         |
| 3      | #4 diários estaduais top 5                                                    |
| 4      | #6 cias_saneamento + #7 aneel_leiloes_pre                                     |
| 5      | #9 secretarias estaduais top 7                                                |
| 6      | #5 petrobras webcast + #8 dnit + folga pra debugar                            |

## O que fica fora deste top 10 (e por quê)

- **LinkedIn vagas**: Atratividade altíssima (sinal #1 antecipado), mas ToS + anti-bot = risco jurídico/técnico fora do escopo pré-launch.
- **Bloomberg/Reuters/FT**: Assinatura R$ 3-5k/mês, 50% overlap com Valor.
- **Cartórios de imóveis (ONR)**: Excelente sinal mas API ONR é B2B paga, requer parceria comercial.
- **Twitter/X jornalistas**: API X $100/mês Basic + risco de banimento. Reavaliar pós-launch.
- **Juntas comerciais**: Captcha + auth em 27 estados = custo de implementação inviável agora.
