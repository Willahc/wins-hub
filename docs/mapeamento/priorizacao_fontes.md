# Priorização ROI vs Complexidade — fontes de obras

> Matriz baseada em `fontes_mapeadas.yaml`. Versão 1.0 · 2026-05-13

## Critério de pontuação

**ROI estimado** = `volume_obras_relevantes_mes × antecipacao_media_semanas × peso_qualidade_decisor`

- Volume: obras/mês que provavelmente cruzam o threshold de Pipeline (R$ 10 M+).
- Antecipação: quanto antes da contratação. Ranges normalizados ao valor médio.
- Peso qualidade decisor: 1.0 (estrutura/oficial), 0.5 (notícia), 0.3 (informal).

**Complexidade** = soma ponderada de:

- Tipo técnico (api_rest 1, rss 1, html_scraper 2, scraper+pdf 3, audio/transcript 4)
- Anti-bot (0 nenhum / 1 leve / 2 forte)
- Cadastro/auth (0/1)
- Multi-fonte (cluster 27 UFs = +2)
- Custo operacional contínuo (0 grátis / 1 baixo / 2 alto)

## Matriz quadrantes

```
                      Complexidade →
                   BAIXA          MÉDIA           ALTA
                ┌──────────────┬───────────────┬───────────────┐
       ALTO     │ QUICK WINS   │ STRATEGIC     │ HIGH-RISK     │
   R           │              │ INVESTMENTS   │ HIGH-REWARD   │
   O           ├──────────────┼───────────────┼───────────────┤
   I           │ NICE TO HAVE │ MODERATE      │ AVALIAR       │
       BAIXO   │              │               │ PULAR         │
                └──────────────┴───────────────┴───────────────┘
```

## QUICK WINS (alto ROI, baixa complexidade)

> Atacar primeiro. Sempre ROI > custo de implementação em semanas.

| Fonte                            | Categoria  | Volume/mês | Antecipação | Complexidade | Razão                                                            |
| -------------------------------- | ---------- | ---------: | ----------- | -----------: | ---------------------------------------------------------------- |
| **compras_gov**                  | Federal    |        ~80 | 4-26 sem    |        BAIXA | API REST oficial, swagger público. Manifestação interesse antecipa edital. |
| **aneel_leiloes** (calendário)   | Federal    |          5 | 8-26 sem    |        BAIXA | HTML simples, ~6 leilões/ano com lista de proponentes (mapa EPC). |
| **dou** (filtro keywords)        | Federal    |        ~50 | 0-4 sem     |        BAIXA | API JSON oficial in.gov.br. querido-diario tem parser pronto.    |
| **diarios_oficiais_municipais**  | Municipal  |       ~200 | 0-26 sem    |        BAIXA | OKBR já entrega ~60% das capitais via querido-diario dataset.    |
| **apex_brasil**                  | Internac.  |         12 | 4-52 sem    |        BAIXA | RSS estatal, sem anti-bot. Bom sinal de IDE.                     |
| **abdi**                         | Internac.  |          8 | 4-52 sem    |        BAIXA | RSS + HTML simples. Programa Nova Indústria.                     |
| **eletrobras_ri** (pós-privat.)  | Federal    |          3 | 4-26 sem    |        BAIXA | HTML simples, alta sinalização individual.                       |
| **aecweb**                       | Setorial   |         15 | 0-4 sem     |        BAIXA | RSS, foco imobiliário industrial + saúde/educação.               |
| **portal_mineracao**             | Setorial   |         12 | 0-4 sem     |        BAIXA | RSS. Complementa MINERACAO além de notícias generalistas.        |

**Custo total estimado QUICK WINS: ~12 dias de engenharia, R$ 0 operacional.**

## STRATEGIC INVESTMENTS (alto ROI, média complexidade)

> Vale o esforço — diferenciam o produto. Atacar segundo onda, 4-6 semanas.

| Fonte                                    | Categoria | Volume/mês | Antecipação | Complexidade | Razão                                                             |
| ---------------------------------------- | --------- | ---------: | ----------- | -----------: | ----------------------------------------------------------------- |
| **diarios_oficiais_estaduais_top5**      | Estadual  |        150 | 0-26 sem    |        MÉDIA | SP/RJ/MG/PR/SC cobrem ~70% do CAPEX nacional.                     |
| **secretarias_estaduais_desenvolvimento**| Estadual  |         40 | 0-12 sem    |        MÉDIA | InvestSP/Invest Minas/Invest BA divulgam IDE antes do anúncio.    |
| **dnit**                                 | Federal   |         30 | 12-104 sem  |        MÉDIA | Editais + ordens de início. PDFs anexos.                          |
| **epe** (PDE)                            | Federal   |          5 | 26-260 sem  |        MÉDIA | Antecipação altíssima (4-10 anos). PDF complexo.                  |
| **ons** (plano transmissão)              | Federal   |          8 | 26-260 sem  |        MÉDIA | dados.ons.org.br tem alguns CSVs. Casa com ANEEL SIGA.            |
| **cias_saneamento** (top 6)              | Estadual  |         25 | 4-52 sem    |        MÉDIA | SABESP+CEDAE+COPASA+SANEPAR+EMBASA+SANEAGO cobrem ~80% mercado.   |
| **petrobras_webcast + plano estratégico**| Informal  |          5 | 0-260 sem   |        MÉDIA | Maior fonte individual de CAPEX (R$ 100 B+/ano). PDF + áudio.     |
| **ri_calls_b3** (top 100)                | Informal  |         30 | 0-12 sem    |        MÉDIA | Considerar parceria com S&P/Eleven em vez de scraping próprio.    |
| **ms_saude** + secretarias               | Federal   |         15 | 12-52 sem   |        MÉDIA | Hospital + UPA. Disperso mas alto ticket.                         |
| **fnde**                                 | Federal   |         80 | 8-26 sem    |        MÉDIA | Escolas + creches. Volume alto, ticket baixo. Útil para construtoras médias. |
| **anac**                                 | Federal   |          2 | 26-104 sem  |        MÉDIA | Baixo volume mas CAPEX altíssimo por obra (terminal/pista).       |
| **defesa_licitacoes**                    | Federal   |         10 | 12-260 sem  |        MÉDIA | ProSub, Tamandaré, KC-390. Volume baixo, ticket bilionário.       |
| **petronect**                            | Federal   |         60 | 2-12 sem    |        MÉDIA | Cadastro + acesso restrito. Vale a pena pelo casamento Petrobras. |
| **aneel_leiloes_pre** (proponentes)      | Federal   |          5 | 8-26 sem    |        MÉDIA | Lista de habilitados = mapa de EPCs antes do leilão.              |

## HIGH-RISK / HIGH-REWARD (alto ROI, alta complexidade)

> Atacar SOMENTE com tempo/orçamento sobrando, ou via parceria.

| Fonte                                  | Categoria | Volume/mês | Antecipação | Complexidade | Razão                                                                                |
| -------------------------------------- | --------- | ---------: | ----------- | -----------: | ------------------------------------------------------------------------------------ |
| **linkedin_vagas**                     | Informal  |         30 | 8-52 sem    |         ALTA | "Buyer Nova Planta X" é sinal #1 antecipado. ToS + anti-bot. Considerar parceria.    |
| **twitter_x_jornalistas + secretarias**| Informal  |         40 | 0-1 sem     |         ALTA | API X paga ($100/mês Basic). Sinal quase realtime.                                   |
| **juntas_comerciais_top5**             | Estadual  |         30 | 4-26 sem    |         ALTA | Aumento de capital ↗ obra. Captcha + auth em JUCESP/JUCEMG.                          |
| **bloomberg_reuters_ft_brazil**        | Internac. |         20 | 0-4 sem     |         ALTA | Assinaturas corporativas R$ 3-5k/mês total. >50% reciclam Valor.                     |
| **cartorios_imoveis_onr**              | Municipal |          5 | 12-52 sem   |         ALTA | API ONR paga, B2B. Compra de terreno = obra em 18 meses.                             |

## NICE TO HAVE (baixo ROI, baixa complexidade)

> Atacar só se já cobriu tudo acima. Volume marginal.

| Fonte                                  | Categoria | Volume/mês | Antecipação | Complexidade | Razão                                                                |
| -------------------------------------- | --------- | ---------: | ----------- | -----------: | -------------------------------------------------------------------- |
| **embaixadas_china_eua_europa**        | Internac. |          5 | 4-26 sem    |        BAIXA | Multi-idioma. Sobreposição com Valor/Bloomberg.                      |
| **construcao_mercado** (PINI)          | Setorial  |          8 | 0-4 sem     |        BAIXA | Mensal. Sobreposição com AECweb.                                     |
| **ibsi** (galpões logísticos)          | Setorial  |          6 | 4-26 sem    |        BAIXA | Nicho data center + e-commerce.                                      |
| **saude_business / setor_saude**       | Setorial  |          8 | 0-4 sem     |        BAIXA | Vale se cliente do setor for prioridade.                             |
| **educacao_brasileira**                | Setorial  |          4 | 8-52 sem    |        BAIXA | Sobreposição com FNDE + diários.                                     |
| **defesanet / tecnodefesa**            | Setorial  |          6 | 0-8 sem     |        BAIXA | Sobreposição com DOU defesa.                                         |
| **agro (Canal Rural / Globo Rural)**   | Setorial  |         12 | 0-4 sem     |        BAIXA | Frigorífico, esmagadora. Vale se cliente AGRO for prioridade.        |
| **valec_infra_sa**                     | Federal   |          3 | 26-104 sem  |        MÉDIA | Sobreposição com ANTT/ANTAQ.                                         |

## AVALIAR / PROVÁVEL PULAR

| Fonte                       | Motivo                                                                 |
| --------------------------- | ---------------------------------------------------------------------- |
| **bcb_credito_direcionado** | Dados agregados, não traz obras individuais.                           |
| **agencias_estaduais_energia_saneamento** | Sobreposição com ANEEL + companhias estaduais. |
| **cartorios_imoveis_individuais** (sem ONR) | Custo de implementação inviável fonte-por-fonte. |
| **investor_day_top50** | Anual, melhor consumir via parceria (S&P) que scraping próprio.        |

## Sumário por quadrante

| Quadrante           | Fontes | Volume estim/mês | Custo eng (sem) | Custo op/mês |
| ------------------- | -----: | ---------------: | --------------: | -----------: |
| QUICK WINS          |      9 |              385 |             2-3 |        R$ 0  |
| STRATEGIC           |     14 |              ~530| 4-8             |        R$ 100-500 |
| HIGH-RISK           |      5 |              ~125| 6-12            |        R$ 4-7k    |
| NICE TO HAVE        |      8 |              ~52 | 1-2 ea.         |        R$ 0  |
| AVALIAR PULAR       |      4 |               ~0 |                 |              |
| **Cobertura ideal** | **36**|        **~1.092**|       **15-25** |   **~R$ 7k** |
