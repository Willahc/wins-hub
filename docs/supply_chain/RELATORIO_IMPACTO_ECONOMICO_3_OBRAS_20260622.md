# Relatório de Impacto Econômico — Investimentos Industriais Estruturantes
### Efeito multiplicador, encadeamento setorial e geração de empregos na cadeia de fornecimento

**Preparado por:** WiNS Hub — Supply Chain Intelligence · **Data:** Junho/2026
**Metodologia:** Matriz de Insumo-Produto IBGE 2015 (Nível 67) — Matriz de Leontief (Tabela 15)
**Escopo:** 3 investimentos âncora — GWM (ES), Petrobras RPBC (SP), Porto Imetame (ES)

---

## 1. Sumário Executivo

| Investimento | UF | CAPEX | Multiplicador de produção | Produção total gerada | Valor adicionado (PIB) | Empregos (dir.+indir.) |
|---|---|---|---|---|---|---|
| **GWM — montadora** | ES | R$ 6,0 bi | **2,18×** | **R$ 13,1 bi** | R$ 5,9 bi | **~78.500** |
| **Petrobras RPBC — biocombustíveis** | SP | R$ 6,0 bi | **2,38×** | **R$ 14,3 bi** | R$ 6,4 bi | **~85.700** |
| **Porto Imetame** | ES | R$ 2,7 bi | **1,59×** | **R$ 4,3 bi** | R$ 1,9 bi | **~38.600** |
| **TOTAL** | — | **R$ 14,7 bi** | — | **R$ 31,7 bi** | **R$ 14,2 bi** | **~202.800** |

> **Leitura:** cada R$ 1,00 investido pela GWM movimenta R$ 2,18 na economia brasileira somando os
> efeitos diretos (a própria montadora) e indiretos (toda a cadeia que a abastece). Os R$ 14,7 bi dos
> três projetos geram **R$ 31,7 bi de produção** e sustentam cerca de **203 mil postos de trabalho**
> diretos e indiretos ao longo da cadeia.

---

## 2. Metodologia (transparente e auditável)

- **Fonte:** Matriz de Insumo-Produto do Brasil, IBGE, referência 2015, nível 67 (67 atividades / 127 produtos). Dado público e gratuito.
- **Multiplicador de produção:** soma da coluna do setor na **Matriz de Leontief** `(I − A)⁻¹` — captura efeito direto + indireto (todas as rodadas de compras intersetoriais).
- **Valor adicionado:** aplicado coeficiente VA/Produção ≈ 0,45 (média da economia brasileira). *Refinável com o vetor de VA setorial da própria MIP.*
- **Empregos:** coeficiente de **postos por R$ 1 milhão de produção** por grupo de atividade — indústria 6, serviços 9, construção 11, agropecuária 14. *Estimativa de ordem de grandeza; a calibração final usa RAIS/PNAD Contínua por setor.*
- **Dimensão UF:** a MIP é nacional. A capacidade de **captação local** é estimada pela presença de fornecedores no estado, por divisão CNAE, na base WiNS Hub (3,97M CNPJs RFB).

⚠️ *Limites:* matriz de 2015 (coeficientes técnicos estruturais, estáveis no tempo, mas não capturam choques recentes de preço); efeito-renda (induzido pelo consumo dos novos empregados) **não** incluído — os números são, portanto, **conservadores**.

---

## 3. GWM — Montadora (Aracruz/ES) — CAPEX R$ 6,0 bi

**Multiplicador 2,18× → R$ 13,1 bi de produção · R$ 5,9 bi de PIB · ~78.500 empregos**

### Encadeamento setorial (onde a produção é gerada)
| Setor da cadeia | Produção gerada |
|---|---|
| Fabricação de automóveis/caminhões/ônibus (direto) | R$ 6.204 mi |
| Peças e acessórios para veículos | R$ 1.212 mi |
| Comércio atacado/varejo | R$ 1.008 mi |
| Transporte terrestre | R$ 410 mi |
| Borracha e plástico | R$ 396 mi |
| Siderurgia / ferro-gusa / aço | R$ 355 mi |
| Refino de petróleo | R$ 276 mi |
| Intermediação financeira/seguros | R$ 274 mi |

### Captação local — Espírito Santo
A base WiNS Hub mostra a capacidade instalada do ES por elo da cadeia:

| Elo da cadeia | Fornecedores no ES | No Brasil | Leitura |
|---|---|---|---|
| Transporte de carga | 24.343 | 1.063.620 | ✅ ES capta integralmente |
| Engenharia/arquitetura | 5.193 | 245.716 | ✅ forte |
| Construção | 3.619 | 199.879 | ✅ forte |
| Produtos de metal | 3.210 | 112.494 | 🟡 parcial |
| Autopeças (CNAE 29) | 4 | 210 | 🔴 quase nulo — importa de SP/PR |
| Siderurgia/aço (CNAE 24) | 0 | 4 | 🔴 fora do estado |

> **Mensagem ao governo do ES:** o estado capta com folga a fase de **construção, logística e
> engenharia** (~R$ 1,8 bi da cadeia), mas a demanda de **autopeças, aço e borracha** (~R$ 2,0 bi)
> vaza para outros estados. **Política pública de atração de fornecedores Tier-1 automotivos** ao
> ES converteria esse vazamento em PIB e emprego locais.

---

## 4. Petrobras RPBC — Biocombustíveis (Cubatão/SP) — CAPEX R$ 6,0 bi

**Multiplicador 2,38× (o maior dos três) → R$ 14,3 bi de produção · R$ 6,4 bi de PIB · ~85.700 empregos**

### Encadeamento setorial
| Setor da cadeia | Produção gerada |
|---|---|
| Fabricação de biocombustíveis (direto) | R$ 6.084 mi |
| **Agropecuária (matéria-prima)** | R$ 2.764 mi |
| Químicos orgânicos/inorgânicos | R$ 616 mi |
| Refino de petróleo | R$ 601 mi |
| Comércio atacado/varejo | R$ 589 mi |
| Transporte terrestre | R$ 556 mi |
| Outros produtos alimentares | R$ 350 mi |

### Captação local — São Paulo
| Elo da cadeia | Fornecedores em SP | No Brasil | Leitura |
|---|---|---|---|
| Agropecuária (matéria-prima) | 151.318 | 154.474 | ✅ SP concentra a oferta agro |
| Transporte de carga | 337.352 | 1.063.620 | ✅ |
| Químicos | 161 | 437 | 🟡 base fina |
| Refino/combustíveis | 151 | 859 | 🟡 |

> **Mensagem:** o multiplicador é o mais alto dos três porque o biorrefino puxa fortemente a
> **agropecuária** (R$ 2,76 bi) — cadeia profundamente enraizada em SP e no Centro-Oeste. É o caso
> mais virtuoso em termos de interiorização do desenvolvimento e renda no campo.

---

## 5. Porto Imetame (Aracruz/ES) — CAPEX R$ 2,7 bi

**Multiplicador 1,59× → R$ 4,3 bi de produção · R$ 1,9 bi de PIB · ~38.600 empregos**

### Encadeamento setorial
| Setor da cadeia | Produção gerada |
|---|---|
| Armazenagem e auxiliares de transporte (direto) | R$ 2.821 mi |
| Transporte terrestre | R$ 145 mi |
| Engenharia/arquitetura | R$ 122 mi |
| Refino/combustíveis | R$ 117 mi |
| Intermediação financeira | R$ 116 mi |
| Comércio | R$ 88 mi |

### Captação local — Espírito Santo
| Elo da cadeia | Fornecedores no ES | No Brasil |
|---|---|---|
| Armazenagem/aux. transporte | 771 | 38.008 |
| Transporte de carga | 24.343 | 1.063.620 |
| Engenharia | 5.193 | 245.716 |

> **Mensagem:** infraestrutura logística tem multiplicador menor (1,59×) que manufatura, mas a cadeia
> é **majoritariamente local** (transporte, engenharia, serviços) — alta retenção do impacto no ES.

---

## 6. Produtos derivados (oferta WiNS Hub)
1. **Relatório de impacto** sob demanda por investimento/estado (este documento) — B2G, consultorias, federações industriais.
2. **Monitor de cadeia ao vivo:** lista de CNPJs no estado por elo, com decisor, para política de
   adensamento de cadeia / atração de fornecedores.
3. **API de impacto:** dado o CAPEX + setor + UF, retorna multiplicador, PIB, empregos e gargalos de
   captação local.

---
*Documento de prova de conceito — números derivados da MIP IBGE 2015. Para versão comercial:
calibrar empregos com RAIS, incluir efeito-renda induzido e VA setorial da própria matriz.*
