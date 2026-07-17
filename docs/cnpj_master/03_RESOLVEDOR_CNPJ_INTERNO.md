# Resolvedor CNPJ Interno (WiNS Hub V2)

O modulo `app/services/cnpj_master_resolver.py` implementa a consulta
prioritaria ao Cadastro Mestre importado da Planilha Mestre oficial.

## Interface

```python
resolve_cnpj(cnpj, required_fields=None, context=None)
```

- `cnpj`: valor bruto com ou sem mascara. Representacoes cientificas integrais
  sao convertidas por `Decimal`; digitos ausentes nunca sao completados.
- `required_fields`: campos canonicos obrigatorios. Na ausencia do argumento,
  sao exigidos `razao_social` e `situacao`.
- `context`: origem, contexto, provedor esperado e controles tecnicos
  `write_log`/`read_only`.

O retorno inclui `status`, identificador normalizado, dados internos, campos
solicitados, encontrados e ausentes, fontes, captadores, confianca,
`consulta_externa_necessaria`, `request_id`, motivo e indicacao de auditoria.

## Estados

1. `FULL_HIT`: a entidade existe e todos os campos obrigatorios estao
   preenchidos. A consulta externa e marcada como evitada.
2. `PARTIAL_HIT`: a entidade existe, seus dados sao reutilizados e o chamador
   pode buscar somente os campos obrigatorios ausentes.
3. `MISS`: o identificador e valido, mas nao existe no Cadastro Mestre, ou o
   resolvedor interno esta indisponivel. O fallback atual permanece permitido.
4. `INVALID`: o tamanho ou os digitos verificadores sao invalidos. O bloqueio
   ocorre antes da feature flag e nenhuma chamada externa e permitida.
5. `SEM_CNPJ`: valor ausente, vazio ou marcador explicito de ausencia. Nao
   consulta mestre nem provedor.
6. `CPF_NAO_APLICAVEL`: representacao de 11 digitos. Nao consulta mestre nem
   provedor e o CPF completo nao e persistido.

## Persistencia e seguranca

- A leitura usa exclusivamente `wins_v2.entidades_lookup`.
- A auditoria faz somente `INSERT` em
  `wins_v2.enrichment_lookup_log`; o resolvedor nao depende de leitura do log.
- O evento inicial e o evento do provedor compartilham `request_id`.
- O log armazena status, contexto, campos, provedor, chamadas evitadas e
  executadas, duracao e erro sanitizado.
- Identificadores pessoais invalidos nao sao persistidos; mensagens com
  marcadores de senha, token, chave ou credencial sao substituidas por texto de
  redacao.
- O modo `read_only` desabilita escrita de log e foi utilizado no replay.
- Os tempos limite de conexao, statement e lock impedem bloqueio prolongado do
  fallback.
- A configuracao padrao do modulo e
  `MASTER_CNPJ_LOOKUP_ENABLED=false`; a ativacao exige variavel explicita.

## Resultado validado

Os testes do resolvedor terminaram com 13/13 aprovados, cobrindo mascara,
cientifico, zeros a esquerda, digitos verificadores, os seis estados,
concorrencia e falha do banco. A prova de producao confirmou `FULL_HIT` sem
rede, cache ou escrita V1 e com uma linha valida de auditoria.
