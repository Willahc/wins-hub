# Relatorio Final — Alimentacao Automatica da Base Mestre V2

**Data:** 2026-07-16  
**Resultado:** APROVADO  
**Feature flag final:** `MASTER_PIPELINE_V2_ENABLED=true`

## Objetivo

Apos cada captura real gravada com sucesso na V1, alimentar automaticamente
`wins_v2` com os campos canonicos aplicaveis da Planilha Mestre (288), sem
atualizar o XLSX a cada captura e sem publicar obras V2.

## Arquitetura

| Componente | Caminho |
|---|---|
| Servico central | `services/master_pipeline_v2.py` → `processar_captura_para_master` |
| Hook fail-safe | `scripts/_master_hook.py` → `notificar_master_v2` |
| SQL minimo | `sql/01_SQL_MINIMO.sql` |
| Export sob demanda | `scripts/exportar_planilha_mestre_v2.py` |
| Reprocessamento | `scripts/reprocessar_falhas_pipeline_v2.py` |
| Deploy runtime | `/root/wins_hub/app/services/` e `/root/wins_hub/app/scripts/` |

### Ponto central de integracao

1. **Primario:** `_pncp_common.inserir_obra_pncp` (familia PNCP completa)
2. **Hooks minimos** pos-commit em captadores de familias principais
3. **Universal:** trigger `trg_obras_pipeline_inbox` em `public.obras` AFTER INSERT
   → `wins_v2.pipeline_inbox` (payload minimo; reprocessavel)

Falha V2 nunca propaga para V1 (`processar_captura_para_master_safe` + fila
`pipeline_falhas`).

## Captadores integrados

| Familia | Fonte / captador | Integracao |
|---|---|---|
| PNCP | pncp_obras, pncp_civil_100k, pncp_full, pncp_consulta, pncp_defesa | `_pncp_common` |
| ObrasGov | obrasgov_100k | hook em `captar_obrasgov_100k` |
| ANEEL | aneel_siga, aneel_transmissao | hooks |
| IBAMA | ibama_sislic | hook bulk |
| BNDES | bndes_financiamento (+ variantes) | hook bulk |
| ANTAQ | antaq_tup | hook bulk |
| CVM | cvm_ipe | hook bulk |
| DOU | dou | hook em `inserir_obra_dou` |
| Licenciamento | recife (servico + inbox) | servico/testes + trigger |
| Setorial | anp_ep etc. | servico/testes + trigger |

Demais captadores cobertos pelo **trigger de inbox** + reprocessamento.

## Testes por familia (namespace `teste_familia`)

Todas OK + DUPLICADO na 2a execucao + versao>=2 com payload alterado:

- pncp_obras
- obrasgov_100k
- aneel_siga
- ibama_sislic
- bndes_financiamento
- antaq_tup
- cvm_ipe
- dou
- recife_licenciamento_100k
- anp_ep

## Captura de validacao pos-ativacao

- V1: `public.obras` recebeu `PNCP:TEST-V1V2-496ee687ef` (id `2a73d765-fedf-4b25-ad2e-b19c970cde88`)
- V2: 1 captura `CAPTURA_NOVA`, 27 valores, 27 evidencias
- 2a execucao V1: `None` (ON CONFLICT); V2 permanece 1 (sem duplicar)
- Inbox: processado
- `publicado_v2=false` sempre
- `obras_validadas` permanece 0 (sem publicacao automatica)

## Regras de negocio respeitadas

- Nao unir projetos por CNPJ/titulo/municipio
- PNCP → contratante (nunca executora)
- ObrasGov → executora
- BNDES → beneficiaria
- DOU/noticias → hint/CAPEX_LLM (nao CAPEX declarado)
- ANEEL potencia → valor_referencia (nao valor_capex)
- Portao intocado
- Sem APIs externas no pipeline
- Marcadores: HISTORICO_IMPORTADO / CAPTURA_NOVA / REPROCESSAMENTO
- Historico 36023 nao reprocessado automaticamente

## Saude

| Check | Antes | Depois |
|---|---|---|
| HTTPS / | 200 | 200 |
| HTTPS /healthz | 200 | 200 |
| API /healthz | 200 | 200 |

## V1

Decisores e cache_brasilapi inalterados nos totais de isolamento.
`public.obras` cresceu apenas pelas capturas controladas de validacao
(esperado). Nenhuma escrita em colunas/tabelas de decisores.

## Feature flag

- Implementacao/testes: `false`
- Final apos testes: `true` (API recriada; DB e Nginx intocados)
