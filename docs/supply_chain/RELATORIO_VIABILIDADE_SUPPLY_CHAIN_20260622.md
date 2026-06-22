# Relatório de Viabilidade — WiNS Hub Supply Chain Intelligence
**Data:** 22/06/2026 · **Fase:** Exploração / PoC (sem deploy em prod) · **Autor:** Claude Code

## TL;DR
**O modelo funciona; a base de dados não comporta o produto diferenciado — ainda.**
A Matriz Insumo-Produto do IBGE foi ingerida e cruza perfeitamente com CNAE. Mas a base de
3,97M fornecedores é **78% construção+transporte** e tem **~zero fabricantes dos insumos
industriais** (cimento, aço, químicos, plásticos) que a própria matriz aponta como a demanda
real de uma obra. Conclusão: **Modelo 3 (impacto econômico) é viável hoje**; **Modelos 1 e 2
(cadeia de insumos) precisam ANTES de aquisição de inventário de fornecedores industriais.**

---

## FASE 0 — o que foi feito (tudo grátis, fora de prod)
- ✅ Baixada a **Matriz Insumo-Produto IBGE 2015, Nível 67** (`.xls`, 1,27MB) via HTTPS.
- ✅ Parseada a **Tabela 11 — Matriz Bn (coeficientes técnicos dos insumos nacionais)**:
  **127 produtos (insumos) × 67 atividades (compradores)**, 2.034 coeficientes >0,001.
  Artefatos: `/home/william/mip_long.csv`, `/home/william/mip_construcao.csv`.
- ✅ **Mapeamento IBGE→CNAE resolvido AUTOMATICAMENTE**: o prefixo de 2 dígitos do código do
  produto nível 67 = divisão CNAE (ex.: 24912 "laminados/tubos de aço"→CNAE 24; 29921 "peças
  veículos"→CNAE 29; 23001 "cimento"→CNAE 23). **Não precisou da tabela de correspondência.**
- ✅ PoC rodada em 3 obras OURO (GWM, Petrobras RPBC, Imetame).

## Achado de modelagem: 2 cadeias diferentes (importante)
A coluna do **setor-fim** da obra dá o que ela compra **OPERANDO** ≠ a cadeia de **CONSTRUÇÃO**.
- GWM (atividade 2991): topo = peças(R$1,14bi), comércio, aço semi(R$195mi), borracha → insumos de **fabricar carros**.
- Petrobras RPBC (1992 biocombustíveis): topo = **cana(R$2,3bi)**, transporte, óleos vegetais, soja → **matéria-prima**, não construção.
- Imetame porto (5280): topo = engenharia, transporte, armazenagem, segurança → **operar o porto**.
- Cadeia de **Construção (atividade 4180)** — a relevante p/ "fornecedores da obra": cimento(23), aço(24), produtos de metal(25), plástico(22), elétricos(27), tintas(20), vidro/cerâmica(23).

→ São **dois produtos distintos**: procurement da obra (col. 4180) vs efeito multiplicador do setor (col. setor-fim, = Modelo 3).

## Scorecard vs critérios de sucesso do brief
| Métrica | Mínimo | Resultado | Status |
|---|---|---|---|
| Setores IBGE→CNAE mapeados | 10/15 | **127/127 (automático)** | ✅ supera |
| Tempo processamento/obra | <5s | **<1s** (matriz 2.034 linhas) | ✅ supera |
| Fornecedores de insumo por obra | 500+ | depende da divisão (ver abaixo) | ⚠️ enganoso |
| % com decisor identificável | 15% | **<<1%** (CRM ~2,6k empresas vs milhões) | ❌ falha |
| Cobertura setores críticos | 70% | **~36%** (4 de 11 divisões têm inventário) | ❌ falha |

## O blocker: viés da base de fornecedores
Top divisões CNAE da base (3,97M): **43 Serviços p/ construção 39,8% · 49 Transporte 26,7% ·
71 Engenharia 6,2% · 41 Edificações 5,0% · 25 Produtos de metal 2,8%**. Manufatura de insumos
industriais é residual:

| Insumo crítico da obra (matriz) | CNAE | Fornecedores na base |
|---|---|---|
| Cimento / vidro / cerâmica | 23 | **0** |
| Metalurgia / aço | 24 | **4** |
| Plástico / borracha | 22 | **1** |
| Químicos / tintas | 20 | 437 |
| Equip. elétricos | 27 | 2.468 |
| Produtos de metal | 25 | 112.494 |
| Edificações/construção | 41 | 199.879 |
| Transporte de carga | 49 | 1.063.620 |

A matriz acerta a demanda (obra precisa de cimento/aço); a base **não tem quem fornece**. E os
fornecedores que temos (41/43/25/49) **já são o público do Comercial atual** — não é inventário novo.

## Veredito por modelo de monetização
- **Modelo 3 — Relatório de impacto econômico (B2G/enterprise): VIÁVEL HOJE.** Só depende da
  matriz (que temos) + CAPEX da obra (que temos). Ex. computável agora: "GWM R$6bi → R$1,14bi de
  demanda de autopeças, R$195mi de aço, R$189mi de borracha no ES". Não exige inventário de fornecedores.
- **Modelo 1 — Assinatura por setor de insumo: BLOQUEADO por inventário.** Vender "obras que
  demandam aço" para fornecedores de aço exige TER fornecedores de aço (temos 4). Falta inventário.
- **Modelo 2 — Licenciamento do grafo de cadeia: PARCIAL.** O grafo obra→setor→insumo é gerável
  (matriz), mas o nível "→CNPJ fornecedor" fica oco nas divisões industriais.

## Recomendação
1. **Lançar Modelo 3 primeiro** (impacto econômico) — entregável imediato, zero dependência de inventário.
2. Para destravar Modelos 1/2: **aquisição dirigida de fornecedores industriais** da RFB filtrando
   CNAE divisões 20–33 (cimento 23, aço 24, químicos 20, plásticos 22, elétricos 27, máquinas 28).
   É um passo de **dados** (ingestão RFB por CNAE), não de modelagem — a modelagem já está provada.
3. O gargalo de **decisor** é idêntico ao Comercial (CRM cobre ~2,6k empresas) — qualquer expansão
   de inventário herda o mesmo custo de enriquecimento (Hunter/descoberta).

## Não feito (fora do escopo desta fase)
- SINAPI (granularidade por tipo de obra) — complementaria a col. 4180 com composições reais.
- Nenhuma tabela criada em prod (`cadeia_insumo_produto`/`matches_cadeia_obra`) — conforme brief.
- Tabela de correspondência oficial IBGE×CNAE não precisou ser baixada (prefixo resolveu).
