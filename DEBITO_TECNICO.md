
## 07/05/2026 — Cleanup completo de janela_bidding (RESOLVIDO)

**Contexto:** Em 06/05 15:36, operação obras_cleanup dropou a coluna obras.janela_bidding 
sem remover referências em main.py (4 pontos) e nos 8 captadores. Estourou em 07/05 
02:00 BRT quando captadores trouxeram payload real.

**Fix aplicado em 07/05 manhã:**
- main.py: removido em 4 pontos (DDL bootstrap, CAMPOS_STANDARD, Pydantic ObraReq, POST /api/admin/obras)
- 8 captadores: removido da lista de colunas + tupla VALUES
- captar_ibama.py: corrigidos índices hardcoded obra[18] → obra[17] (efeito colateral)
- ALTER TABLE obras DROP COLUMN janela_bidding (definitivo — coluna já estava dropada desde 06/05 15:36, comando virou no-op)

**Débito técnico anotado pra futuro:**
- captar_ibama acessa tupla por índice numérico (obra[N]) — frágil. Refatorar pra dict 
  ou named tuple quando houver tempo (não-urgente).

**Backups disponíveis:**
- DB: /root/backups/wins_hub/wins_hub_pre_cleanup_jb_20260507_1211.sql.gz
- main.py: /root/wins_hub/app/main.py.bak_pre_cleanup_jb_20260507_1211
- Captadores: /root/backups/captadores/*.bak_pre_cleanup_jb_20260507_1211

---

## Captador IBAMA — duplicacao por id_externo (descoberto 07/05/2026)

**Contexto:** Durante FASE 3B.1 do pipeline de obras Ouro, identificamos que o captador IBAMA insere uma linha de obra distinta para cada licenca emitida (LP, LI, LO) do mesmo empreendimento fisico.

**Causa-raiz:** UNIQUE constraint do captador e em `id_externo`, e o IBAMA SISLIC gera id_externo distinto pra cada licenca (sufixos `XXXXX/AAAA` diferentes pro mesmo numero de processo).

**Exemplo concreto:** Projeto Novas Minas (MRN) tem 2 entradas no banco:
- `3c79d1b0-...` — Licenca Previa (sufixo `00697/2024`)
- `1b896c1d-...` — Licenca de Instalacao (sufixo `01555/2026`)
- Mesmo numero de processo IBAMA: `02001.029328/2018-61`

**Impacto:** Provavelmente dezenas ou centenas de obras 'duplicadas' no banco hoje, todas representando licencas sequenciais do mesmo empreendimento.

**Solucao proposta (Opcao D do dia 07/05):**
1. Refatorar captador IBAMA pra extrair numero de processo via regex no id_externo
2. Modelar como 'obra mae' + tabela de licencas (timeline de fases)
3. Reprocessar 25k+ obras existentes
4. Migrar relacionamentos (matches, interacoes) pra obra mae

**Nao-urgencia:** Lead_score ja prioriza LI sobre LP automaticamente. UI suporta ambas convivendo.

**Workaround atual (07/05/2026):** Opcao A — manter ambas entradas no banco com mesmo canal_cadastro_url. Aplicado nas obras MRN.

**Trigger pra resolver:** quando aparecer a primeira reclamacao de cliente sobre 'obra duplicada' no dashboard, OU quando o captador IBAMA tiver outro motivo pra ser refatorado, OU pre-roadmap de Hyper Ouro.

---

## Setor 'GOVERNO' sem mapeamento de categorias (decisão 07/05/2026)

**Contexto:** Durante FASE 3B.3B, foi inserida a obra MANUAL-NEWS-2026-05-07-10 (FGAM — Fundo Garantidor da Atividade Mineral, União Federal) com `setor='GOVERNO'`.

**Decisão:** O setor 'GOVERNO' permanece **sem mapeamento** em `setor_categorias`. Razão: o FGAM é um fundo regulatório em tramitação no Congresso, não uma obra física com fornecedores físicos a contratar. Não faz sentido tentar fazer matchmaking aqui.

**Implicação:** Esta obra aparece no banco como sinal estratégico (R$5bi tramitando em fundo público), mas não gera matches automáticos.

**Trigger pra revisitar:** Se aparecer outra obra com setor='GOVERNO' que **seja** uma obra física (ex: construção de prédio público), revisitar a decisão.

**Achado correlato (07/05/2026):** Setor `SUCROENERGETICO` (15 obras) também aparece sem mapeamento em `setor_categorias` — não foi tratado neste fix porque está fora do escopo do bug B (PETROLEO_GAS). Avaliar mapeamento separado quando houver demanda.
