# Relatorio Executivo - Cadastro Mestre de CNPJ (carga COMPLETA)

## Resultado

**APROVADO** em 16 de julho de 2026.

A carga dos 7.653 CNPJs validos da Planilha Mestre COMPLETA foi concluida em
`wins_v2.entidades_lookup`. O resolvedor interno responde `FULL_HIT` sem
provedor externo para CNPJs presentes no cadastro mestre.

## Reconciliacao oficial

- Planilha: `PLANILHA_MESTRE_WINS_HUB_V2_COMPLETA.xlsx`
- SHA-256: `4f77e664350d8dbab785bd3b734180f1057af9e2f4f7a50ebcf17a34cd8b8cca`
- Linhas na aba `02_ENTIDADES_CNPJ_MESTRE`: 7.687
- Validos unicos: 7.653
- Invalidos (DV): 34 (rejeitados; nao importados)
- 1a importacao: 7.647 inseridos + 6 atualizados
- 2a importacao: 0 inseridos + 0 atualizados + 7.653 ignorados
- Contagem final: `entidades_lookup = 7653`, `cnpjs_unicos = 7653`

## Provas de runtime

| Caso | CNPJ | Status | API externa |
|---|---|---|---|
| Existente | `60509015000101` | FULL_HIT | nao (`provedor_externo=None`, `consulta_externa_necessaria=False`) |
| Existente | `10985639000127` | FULL_HIT | nao |
| Ausente valido | `00000000000191` | MISS | fallback legado habilitado |
| Invalido | `00000000000000` | INVALID | nao |

## Saude e isolamento

- Site HTTPS `/`: HTTP 200
- `/healthz` (HTTPS): HTTP 200
- API `/healthz` (`:8001`): HTTP 200
- V1: contagens e soma de `xmin` de `public.obras`, `public.decisores_obra`,
  `public.cache_brasilapi` e `public.fornecedores` permanecem alinhadas ao
  fingerprint de isolamento (sem escrita V1)
- Permissoes finais: `wins_app` somente `SELECT` em `entidades_lookup`

## Artefatos

- Importador: `02_IMPORTADOR_PLANILHA_MESTRE.py`
- Reconciliacao: `17_RECONCILIACAO_PLANILHA.md`
- Manifesto: `13_MANIFESTO.sha256`
