# Integracao do Resolvedor no Pipeline

## Servico principal

`app/services/brasilapi.py` consulta o Cadastro Mestre antes do cache e da
BrasilAPI. No `FULL_HIT`, o payload interno e convertido para o contrato legado
e retornado sem acessar cache, provedor ou tabelas V1. No `PARTIAL_HIT`, os
campos internos sao mesclados primeiro; cache e rede so sao considerados se
ainda faltar campo obrigatorio. `MISS` mantem o fallback atual; `INVALID`,
`SEM_CNPJ` e `CPF_NAO_APLICAVEL` retornam sem rede. Quando o chamador nao
declara campos, o servico exige conservadoramente o contrato amplo da
BrasilAPI, evitando suprimir QSA, telefone, CNAE ou endereco de fluxos legados.
Falha de cache/persistencia e best-effort e nao impede o fallback nem descarta
um payload HTTP 200.

## Sales Intelligence

`app/sales_intelligence/camada1_identificacao/brasilapi.py` preserva a
assinatura de `buscar_dados_cnpj` e o formato esperado pela Camada 1. A lista de
campos necessarios e declarada no adaptador, permitindo diferenciar
`FULL_HIT` de `PARTIAL_HIT` sem inventar dados ausentes.

## Fila e job automatico

- `app/scripts/enriquecer_fila.py` aplica o resolvedor antes de BrasilAPI e
  Serper, preservando os retornos assincronos do worker.
- `app/scripts/enrichment_auto_job.py` aplica o mesmo controle antes de
  publica.cnpj.ws, ReceitaWS e da consulta de QSA via servico BrasilAPI.
- Cada provedor externo registra sua propria tentativa, sucesso ou falha,
  correlacionada com a decisao interna.

## Cron diario de promocao

O cron root das 06:00 executa
`app/scripts/promover_pipeline_via_brasilapi.py --commit --limit 30`. O script
agora usa `services.brasilapi.consultar_cnpj_com_erro`, declara QSA como campo
obrigatorio e informa contexto e provedor. A implementacao nao possui
`urlopen` nem URL direta para a BrasilAPI.

A auditoria exaustiva dos caminhos ativos confirmou que esse era o unico
bypass automatico adicional. Rotinas manuais e o Portao permaneceram fora do
escopo e sem alteracao.

## Evidencias de integracao

- O teste mockado de `PARTIAL_HIT` executou exatamente uma chamada externa.
- A prova de `FULL_HIT` configurou spies que falhariam em qualquer acesso de
  rede, cache ou escrita V1; todos permaneceram com contagem zero.
- Os adaptadores principal, Sales Intelligence, fila e job automatico foram
  aprovados nos testes de runtime.
- Falha de conexao/cache, falha de persistencia e cache negativo foram testados
  sem perda de fallback ou repeticao externa indevida.
- O cron de promocao passou por 7 casos de mapeamento local. Na prova com dados
  reais do mestre, produziu `PARTIAL_HIT`, uma unica chamada externa mockada,
  zero escrita V1 e dois eventos de log correlacionados por um `request_id`.
- A prova em producao gerou uma linha valida em
  `wins_v2.enrichment_lookup_log` com chamada externa evitada.

## Ativacao

Depois de todos os gates, `MASTER_CNPJ_LOOKUP_ENABLED=true` foi aplicado por
recriacao somente do container da API. Os containers de banco e Nginx
mantiveram os mesmos IDs, a API e o site responderam HTTP 200 e nenhuma escrita
foi observada nas quatro tabelas V1 auditadas.
