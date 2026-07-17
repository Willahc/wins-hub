# Inventario de Chamadas Externas de CNPJ

Este inventario registra os pontos ativos do WiNS Hub Comercial que consultam
provedores externos de dados cadastrais. Todos foram mantidos no fluxo
existente e passaram a consultar `resolve_cnpj()` antes do provedor.

| Arquivo | Funcao | Provedor externo | Integracao interna |
|---|---|---|---|
| `app/services/brasilapi.py` | `consultar_cnpj_com_erro` | BrasilAPI | Resolve primeiro no Cadastro Mestre; `FULL_HIT` retorna sem cache, rede ou escrita V1; `PARTIAL_HIT` e `MISS` preservam o fallback atual. |
| `app/sales_intelligence/camada1_identificacao/brasilapi.py` | `buscar_dados_cnpj` | BrasilAPI | Reutiliza os campos internos e solicita externamente apenas os obrigatorios ainda ausentes. |
| `app/scripts/enriquecer_fila.py` | `brasil_api` | BrasilAPI | Bloqueia identificador invalido, evita rede no `FULL_HIT` e mescla dados internos no `PARTIAL_HIT`. |
| `app/scripts/enriquecer_fila.py` | `serper_descobrir` | Serper | Consulta o Cadastro Mestre antes da pesquisa e registra a decisao e o resultado do provedor. |
| `app/scripts/enrichment_auto_job.py` | `discover_domain_via_cnpj` | publica.cnpj.ws e ReceitaWS | Executa o resolvedor antes da cadeia de provedores e registra cada tentativa externa separadamente. |
| `app/scripts/enrichment_auto_job.py` | `_brasilapi_qsa` | BrasilAPI | Usa o servico integrado com os campos obrigatorios de QSA, preservando o contrato do job. |
| `app/scripts/promover_pipeline_via_brasilapi.py` | promocao do pipeline | BrasilAPI | O cron root diario das 06:00 usa `consultar_cnpj_com_erro` com QSA obrigatorio e contexto de auditoria; nao possui `urlopen` nem URL direta. |

## Regras verificadas

- `FULL_HIT`: zero chamadas externas e zero acessos ao cache/V1.
- `PARTIAL_HIT`: os dados internos sao preservados e o provedor e chamado uma
  unica vez somente quando permanece campo obrigatorio ausente.
- `MISS`: o fallback existente permanece ativo.
- `INVALID`: nenhum provedor externo e acionado.
- `SEM_CNPJ` e `CPF_NAO_APLICAVEL`: encerram antes de leitura do mestre ou
  provedor; o CPF completo nao e registrado.
- Chamadores sem `required_fields` usam o contrato amplo da BrasilAPI para nao
  transformar um cadastro parcial em `FULL_HIT` indevido.
- Cada decisao e cada tentativa de provedor e correlacionada por `request_id`
  em `wins_v2.enrichment_lookup_log`.
- As assinaturas publicas e os formatos de retorno anteriores foram
  preservados para os chamadores atuais.

A auditoria exaustiva encontrou apenas esse cron como bypass automatico
adicional. As demais chamadas diretas pertencem a rotinas manuais ou ao Portao,
que permaneceu fora do escopo e nao foi alterado.
