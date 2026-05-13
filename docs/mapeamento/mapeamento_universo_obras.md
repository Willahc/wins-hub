# Mapeamento do universo de fontes de obras — WiNS Hub

> Documento consolidado executivo. Versão 1.0 · 2026-05-13
>
> Resposta à pergunta: "**Onde estão TODAS as obras/investimentos relevantes no Brasil, e como chegar até elas?**"

---

## Sumário executivo

### Cobertura atual

Hoje o WiNS Hub roda **9 captadores ativos** + 1 captador notícias multi-fonte:

| Captador atual              | Cobertura                              | Volume típico |
| --------------------------- | -------------------------------------- | -------------: |
| ibama                       | Licenciamento ambiental federal        | ~12 obras/dia |
| bndes                       | Operações contratadas                  | ~67/dia       |
| aneel                       | Geração + transmissão (SIGA)           | ~86/dia       |
| antt                        | Concessões rodo/ferro                  | ~50/dia       |
| antaq                       | Terminais portuários                   | ~55/dia       |
| anm                         | Direitos minerários (CFEM)             | ~1.880/dia    |
| cvm                         | Fatos relevantes B3                    | ~16/dia       |
| cimm                        | Notícias mineração                     | ~15/dia       |
| agenciainfra                | Editorial infra                        | ~79/dia       |
| noticias_setoriais          | RSS multi-fonte + Haiku                | ~0-5/dia      |

**Setores BEM cobertos:** Energia, Mineração, Infraestrutura linear, Portuário.
**Setores SUB-cobertos:** Industrial (depende quase só de notícia), Logística, Agro.
**Setores SEM cobertura:** Saneamento, Saúde, Educação, Defesa, Data Center, Imobiliário industrial.

### Cobertura ideal (após top 10)

Implementando o [top 10 recomendado](./top10_recomendado.md):

| Indicador                            | Atual         | Pós top 10    | Δ      |
| ------------------------------------ | ------------: | ------------: | -----: |
| Fontes ativas                        |            10 |            20 | +10    |
| Obras Pipeline/mês                   |          ~600 |        ~1.200 | +100%  |
| Antecipação média (semanas)          |             6 |            12 | +6 sem |
| Setores com cobertura primária       |             5 |             8 | +3     |
| Custo operacional adicional          |               |   R$ 100-500/mês | mínimo |

---

## 1. Definição formal de "Obra"

→ Documento separado: [`definicao_obra.md`](./definicao_obra.md)

Resumo: R$ 10 M+ entra na base; R$ 100 M+ é Ouro elegível com decisor. Workflow `rumor → anunciado → licenciado → contratado → em_obra → operacional`. 12 tipos cobertos.

---

## 2. Universo completo de fontes mapeadas

→ Catálogo completo: [`fontes_mapeadas.yaml`](./fontes_mapeadas.yaml)

**51 entradas catalogadas** (algumas representam clusters de 27 UFs ou 50 cidades, totalizando ~200 fontes individuais):

| Categoria              | Entradas no YAML | Fontes individuais (estim.) | Status hoje |
| ---------------------- | ---------------: | --------------------------: | ----------- |
| Federal                |               22 |                          22 | 9 cobertas, 13 não |
| Estadual               |                5 |        ~120 (4 × 27 UFs)     | 0 cobertas |
| Municipal              |                2 |    ~50 (50 cidades)          | 0 cobertas |
| Setorial notícias      |               12 |                          12 | 5 cobertas, 7 não |
| Informal               |                6 |                           6 | 0 cobertas |
| Internacional          |                4 |                           4 | 0 cobertas |
| **TOTAL**              |           **51** |                    **~214** | **14 cobertas (~6.5%)** |

---

## 3. Matriz de priorização ROI × complexidade

→ Detalhes: [`priorizacao_fontes.md`](./priorizacao_fontes.md)

```
                      Complexidade →
                   BAIXA          MÉDIA           ALTA
                ┌──────────────┬───────────────┬───────────────┐
       ALTO     │ QUICK WINS   │ STRATEGIC     │ HIGH-RISK     │
   R           │ 9 fontes     │ INVESTMENTS   │ HIGH-REWARD   │
   O           │ 385 obras/m  │ 14 fontes     │ 5 fontes      │
   I           │              │ 530 obras/m   │ 125 obras/m   │
               ├──────────────┼───────────────┼───────────────┤
       BAIXO   │ NICE TO HAVE │ MODERATE      │ AVALIAR       │
               │ 8 fontes     │               │ PULAR         │
               │ 52 obras/m   │               │ 4 fontes      │
                └──────────────┴───────────────┴───────────────┘
```

**Cobertura ideal pós-execução de QUICK WINS + STRATEGIC:**
- **23 fontes ativas** (incluindo as 14 atuais)
- **~1.092 obras/mês** entrando no pipeline
- **15-25 semanas-dev** de esforço total
- **R$ ~7k/mês** custo operacional adicional (Haiku + opcional Twitter API)

---

## 4. Top 10 recomendado pré-launch

→ Detalhes + sequenciamento: [`top10_recomendado.md`](./top10_recomendado.md)

| #  | Fonte                                | Ticket | Antecipação | Cobertura nova /mês | Esforço (dias) |
| -: | ------------------------------------ | ------ | ----------: | ------------------: | -------------: |
| 1  | compras_gov (consulta pública)       | médio  |   4-26 sem  |                ~80  |              5 |
| 2  | querido-diario municipais top 50     | médio  |   0-26 sem  |               ~200  |              4 |
| 3  | dou (filtro keywords)                | alto   |    0-4 sem  |                ~50  |              6 |
| 4  | diários estaduais top 5              | alto   |   0-26 sem  |               ~150  |              8 |
| 5  | petrobras webcast + plano            | bilhão |  0-260 sem  |                  ~5 |              7 |
| 6  | cias saneamento top 6                | alto   |   4-52 sem  |                ~25  |              7 |
| 7  | aneel leilões pré-resultado          | alto   |   8-26 sem  |                  ~5 |              4 |
| 8  | dnit obras rodoviárias               | alto   | 12-104 sem  |                ~30  |              8 |
| 9  | secretarias est. desenvolvimento (7) | alto   |   0-12 sem  |                ~40  |              7 |
| 10 | eletrobras_ri + apex                 | alto   |   4-52 sem  |                ~15  |              3 |
|    | **TOTAL**                            |        |             |            **~600** |       **~60**  |

**Sequenciamento sugerido:** sprint de 6 semanas, 2 fontes/semana em paralelo (1 dev focado).

---

## 5. O que fica intencionalmente fora

| Fonte / Categoria                    | Por quê pular agora                                                |
| ------------------------------------ | ------------------------------------------------------------------ |
| LinkedIn vagas (Compras/Buyers)      | Risco jurídico (ToS) + custo proxy residencial. Pós-launch reavaliar via parceria com agregadores. |
| Twitter/X jornalistas + secretarias  | API X $100/mês Basic + risco de banimento. Pós-launch.            |
| Cartórios imóveis (ONR)              | API B2B paga, requer contrato comercial. Pós-launch.              |
| Juntas comerciais (27 UFs)           | Captcha + auth em cada estado. Custo de implementação inviável.   |
| Bloomberg / Reuters / FT             | Assinatura R$ 3-5k/mês. 50% overlap com Valor já capturado.       |
| BCB crédito direcionado              | Dados agregados, não traz obras individuais.                       |
| Investor Day top 50                  | Anual; melhor via parceria (S&P/Eleven) que scraping próprio.     |

---

## 6. Riscos & dependências

### Operacionais

- **Haiku custo:** +R$ 100-500/mês com os filtros adicionais. Mitigação: filtros regex pesados antes do Haiku.
- **Querido-diario dependência:** OKBR mantém dataset gratuito mas projeto comunitário. Mitigação: ter parser próprio como fallback para top 5 capitais.
- **Petrobras webcast:** áudio requer Whisper (~R$ 0.50/call, ~5 calls/ano = R$ 2.50/ano).

### Técnicos

- **PDFs imagem em diários:** ~20% requerem OCR. Mitigação: Tesseract + Haiku para campos críticos.
- **Anti-bot em alguns portais:** SP/RJ relativamente abertos; MG/PR/SC mais agressivos. Mitigação: rate-limit conservador + retry com backoff.

### Legais

- **Diários oficiais:** dados públicos por lei. Sem risco.
- **Compras.gov.br:** API oficial autorizada. Sem risco.
- **DOU:** dados públicos. Sem risco.
- **Notícias setoriais:** Fair use para extração de fatos. Mitigação: armazenar só fatos extraídos, não conteúdo bruto.

---

## 7. Indicador de sucesso

KPIs pra medir 30 dias após implementação de cada quick win:

| KPI                                   | Meta                                       |
| ------------------------------------- | ------------------------------------------ |
| Obras Pipeline/mês                    | Subir de ~600 para ~1.200                  |
| % obras Ouro (com decisor)            | Manter ≥ 5% do Pipeline                    |
| Setores cobertos primariamente        | Subir de 5 para 8                          |
| Latência mediana fonte → DB           | Manter ≤ 24h                               |
| Custo Haiku/100 obras válidas         | Manter ≤ R$ 0.30                           |
| Antecipação mediana fonte → contrato  | Subir de 6 para 12 semanas                 |

---

## Próximos passos sugeridos

1. **Aprovar este documento** (ou ajustar prioridades).
2. **Bater com cliente piloto Luxor:** quais dos setores novos (SANEAMENTO/SAÚDE/EDUCAÇÃO/DEFESA) interessam mais? Re-priorizar top 10 se necessário.
3. **Sprint planning 6 semanas:** alocar dev e iniciar pelo #1 compras_gov (menor risco, maior ROI/dia).
4. **Pós sprint:** reavaliar HIGH-RISK quadrant (LinkedIn vagas, Twitter/X, cartórios ONR) com base no orçamento e parcerias disponíveis.

## Anexos

- [definicao_obra.md](./definicao_obra.md)
- [fontes_mapeadas.yaml](./fontes_mapeadas.yaml)
- [priorizacao_fontes.md](./priorizacao_fontes.md)
- [top10_recomendado.md](./top10_recomendado.md)
