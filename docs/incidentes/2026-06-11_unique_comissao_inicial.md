# 2026-06-11 — UNIQUE parcial comissoes INICIAL (idempotência webhook MP)

## Contexto e autorização
Auditoria 11/06 (fix #1c): reentrega de webhook MP podia criar comissão
INICIAL duplicada (`eh_primeira_assinatura` continua True na 2ª entrega).
Camadas de defesa aplicadas no código (já deployadas): FOR UPDATE +
early-return em `pagamento_webhook` (main.py) e check-before-insert em
`services/comissoes.py`. Este índice é a garantia final no banco.
Autorização do William: "COMMIT" explícito em 11/06/2026 ~10:05 UTC.
Aplicado: CREATE INDEX 8.4ms, B5 OK (índice presente em pg_indexes).
Log: /root/.faxina_log/unique_comissao_inicial_20260611.txt

## SQL exato
```sql
BEGIN;
CREATE UNIQUE INDEX uq_comissoes_inicial_por_prestador
    ON comissoes (prestador_id)
 WHERE tipo = 'INICIAL';
COMMIT;
```
Arquivo: migrations/20260611_unique_comissao_inicial_COMMIT.sql

## Linhas afetadas (SELECT before, dry-run 11/06 10:01)
Tabela `comissoes` vazia (0 rows em todos os tipos). Zero duplicatas
INICIAL. DDL puro, nenhuma linha de dado alterada.

## Resultado esperado pós-commit
- Índice `uq_comissoes_inicial_por_prestador` presente em pg_indexes.
- 2º INSERT INICIAL do mesmo prestador → unique_violation (capturada
  pelo SAVEPOINT comissao_sp do webhook; pagamento não quebra).
- AVULSO/RECORRENTE duplicados continuam permitidos.
Dry-run validou os 3 comportamentos (NOTICEs OK, ROLLBACK limpo, 8ms).
