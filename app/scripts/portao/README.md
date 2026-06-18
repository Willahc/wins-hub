# Portão de Entrada — `app/scripts/portao/`

Filtro + enriquecimento **antes** de qualquer INSERT em `obras`, de qualquer fonte.
Princípio: nada entra sem passar pelo portão; o portão **enriquece inline e só então decide**
(não avalia a linha crua e descarta). Zero validação manual depois.

## Conteúdo (Fase 0 — entregue)
```
portao/
├── obra_classificacao.yaml   # FONTE ÚNICA do critério é-obra/não-é-obra + políticas (decisões 18/06)
├── README.md                 # este arquivo
└── regression/               # harness de regressão (roda antes de cada deploy de fase)
    ├── run_harness.sh        #   runner: pré-reqs + baselines + invariantes + casos
    ├── baselines.md          #   números de referência medidos em 2026-06-18
    ├── casos_decisao.yaml    #   spec executável: input -> veredito esperado do portão
    └── sql/                  #   baselines versionados (funil, sanidade, motivos, resolução interna)
```

## Fluxo do portão (alvo das Fases 1–3)
0. Adapter da fonte → dict comum
1. NOTÍCIA: Haiku extrai {cnpj,valor,setor,empresa}; faltou 1 → **HARD-REJECT** (não insere)
2. PORTÃO-DURO (rejeição = definição de obra válida): não-é-obra / setor OUTRO / CNPJ inválido-guarda-chuva / dup
3. ENRIQUECE INLINE — **Fase 0 interna primeiro** (fornecedores → decisores_preservados → empresa_dominios),
   só o que sobrar vai a web_search free-first → BrasilAPI. **Hunter nunca inline** (batch noturno).
4. TIER (soft, nunca rejeita): OURO/PRATA c/ decisor · BRONZE sem decisor · PIPELINE se só-estimativa
5. PERSISTE só o aprovado; triggers atuais cuidam do pós-insert.

## Decisões fechadas (18/06) — viram casos de teste
- Notícia sem os 4 campos → hard-reject (não insere).
- Hunter só no batch noturno (nunca inline).
- Dumpers crus de RSS aposentados direto (ver `obra_classificacao.yaml: dumpers_aposentar`).

## Como rodar
```bash
bash regression/run_harness.sh          # imprime métricas e DRIFT
bash regression/run_harness.sh --strict # falha (exit!=0) se invariante quebrar — usar em CI/pré-deploy
```

## Próximas fases
- **Fase 1**: `portao.py` (estágios 2–4) plugado em 2 captadores piloto em SHADOW/dry-run; harness passa a rodar `casos_decisao.yaml` contra a função real.
- **Fase 2**: liga nas notícias + aposenta dumpers.
- **Fase 3**: liga nos oficiais com enrich-inline.
- **Fase 4**: faxineiro do limbo (171 NULL) + monitor de yield por fonte.
