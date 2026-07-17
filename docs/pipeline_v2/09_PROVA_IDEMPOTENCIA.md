# Prova de Idempotencia

## Criterio

Chave logica: `(fonte_id, namespace, id_externo, hash_conteudo)`.

## Evidencias

1. **Familias (10):** 1a execucao `OK`, 2a execucao `DUPLICADO`, payload
   alterado gera `OK` com `versao >= 2`.
2. **Validacao V1+V2:** `inserir_obra_pncp` duas vezes:
   - 1a: retorna UUID V1 + 1 captura V2
   - 2a: retorna `None` (ON CONFLICT V1) e V2 permanece com 1 registro
3. Indice unico: `uq_capturas_brutas_dedup`
4. Versao de payload: tabela `wins_v2.capturas_versoes` quando hash muda
