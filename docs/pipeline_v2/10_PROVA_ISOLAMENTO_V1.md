# Prova de Isolamento da V1

## Metodo

Fingerprint de contagem e soma de `xmin` em tabelas V1 criticas, antes e
depois dos testes do pipeline e da ativacao da flag.

## Resultado

| Tabela | Observacao |
|---|---|
| `public.decisores_obra` | contagem e xmin inalterados |
| `public.cache_brasilapi` | contagem e xmin inalterados |
| `public.obras` | +N apenas por capturas **controladas** de validacao (INSERT real de teste) |
| `wins_v2.obras_validadas` | permaneceu 0 (sem publicacao V2) |
| Portao | nao acionado pelo pipeline |

O pipeline grava exclusivamente em objetos do schema `wins_v2` (capturas,
evidencias, entidades, inbox, falhas, lookup via funcao SECURITY DEFINER).

Hooks capturam excecoes; falha V2 nao faz rollback da V1.
