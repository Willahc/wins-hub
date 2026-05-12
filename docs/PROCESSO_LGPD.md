# Processo de Atendimento a Solicitações LGPD

**Controlador:** William Nunes da Silva (MEI · CNPJ 66.528.102/0001-92)
**Email canal único:** `contato@winshubcomercial.com.br`
**SLA legal:** 15 dias (LGPD art. 19)
**Última revisão:** 2026-05-09

---

## 1. Identificação de uma solicitação LGPD

Uma mensagem deve ser tratada como solicitação LGPD se mencionar:
- "Direitos do titular" / "art. 18" / "LGPD"
- Pedidos de **acesso**, **exclusão**, **portabilidade**, **correção**, **anonimização**, **revogação de consentimento**, **informação sobre compartilhamento**
- Remoção de dados de decisor (mesmo sem citar LGPD — base legal é legítimo interesse art. 7º IX, mas o titular tem direito de oposição)

### Triagem no Gmail (manual — fazer 1x)

No Gmail/Hostinger Mail de `contato@winshubcomercial.com.br`:

1. **Criar label "LGPD"** (Configurações → Labels → Create new label)
2. **Criar filtro automático**:
   - Settings → Filters & Blocked Addresses → Create new filter
   - Subject contains: `LGPD OR "direitos titular" OR portabilidade OR exclusão OR "art. 18" OR "remover dados"`
   - From: any
   - Action: Apply label "LGPD" + Star + Never send to spam

---

## 2. SLA

| Etapa | Prazo |
|---|---|
| Confirmação de recebimento ao titular | 24h |
| Resposta final (atendimento ou recusa fundamentada) | **15 dias** (LGPD art. 19) |
| Exclusão efetiva dos dados após pedido confirmado | 30 dias (políticas internas) |
| Retenção de logs de auditoria pós-atendimento | 5 anos (CDC + obrigações fiscais) |

---

## 3. Templates de resposta

### 3.1 Confirmação de recebimento (enviar em até 24h)

```
Assunto: Recebemos sua solicitação LGPD — Protocolo {YYYY-MM-DD-NNN}

Olá {Nome},

Recebemos sua solicitação relacionada a direitos do titular previstos na
LGPD (art. 18). Estamos analisando o pedido e retornaremos com a resposta
final em até 15 dias, conforme art. 19 da Lei 13.709/2018.

Protocolo: {YYYY-MM-DD-NNN}
Tipo de solicitação: {acesso | exclusão | portabilidade | correção | outro}

Caso precise nos enviar informações adicionais (ex.: documento de
identidade pra confirmar identidade do titular), responda este email.

Atenciosamente,
William Nunes da Silva
Controlador / Encarregado de Dados
WiNS HUB · contato@winshubcomercial.com.br
```

### 3.2 Atendimento de pedido de **acesso** (art. 18, II)

Anexar JSON exportado (ver seção 5.1). Mensagem:
```
Olá {Nome},

Conforme solicitado, segue em anexo o relatório completo dos dados
pessoais que tratamos em seu nome na plataforma WiNS HUB:

- {arquivo.json} — exportação estruturada (legível por máquina e humano)

Caso identifique algum dado incompleto ou inexato, você tem o direito
de solicitar correção (art. 18, III). Responda este email com as
correções desejadas.

Atenciosamente,
William
```

### 3.3 Atendimento de pedido de **exclusão** (art. 18, VI)

```
Olá {Nome},

Conforme solicitado, executamos a exclusão dos seus dados em nossa
plataforma na data {DATA}. Pontos de atenção:

- Sua conta foi desativada em {DATA}.
- Dados pessoais (nome, telefone, CNPJs vinculados, histórico de
  acesso) serão purgados em até 30 dias.
- Registros financeiros (notas fiscais, comprovantes de pagamento)
  permanecem retidos por 5 anos por obrigação tributária (CFC NBC TG)
  — após esse prazo são também eliminados.
- Confirmações de decisores que você contribuiu ficam anonimizadas
  (sem vínculo ao seu ID) na base, conforme política de retenção
  agregada de qualidade.

Se preferir conservar os dados em vez de excluir (ex.: férias), nos
avise e podemos suspender a conta sem purga.

Atenciosamente,
William
```

### 3.4 Atendimento de pedido de **portabilidade** (art. 18, V)

Mesmo procedimento de acesso (3.2), mas explicar formato:
```
Olá {Nome},

Segue em anexo a portabilidade dos seus dados em formato JSON
estruturado (compatível com importação programática) para envio
a outro fornecedor de sua preferência.

Cada arquivo representa uma tabela:
- prestador.json — dados de cadastro
- empresas.json — empresas vinculadas
- desbloqueios.json — histórico de desbloqueios
- contribuicoes.json — confirmações que você fez

Atenciosamente,
William
```

### 3.5 Recusa fundamentada (art. 18, §4º)

Cabe recusa se:
- O dado não pertence ao titular (ex.: "quero excluir dados da empresa X" sem ser representante legal)
- O dado está protegido por outra base legal incompatível (ex.: registros fiscais por obrigação legal — art. 7º, II)
- Pedido manifestamente abusivo ou repetido em curto prazo

```
Olá {Nome},

Analisamos seu pedido de {tipo}. Não podemos atendê-lo no momento pelos
seguintes motivos: {explicação clara, em linguagem não-técnica}.

Você tem direito de petição à ANPD (Autoridade Nacional de Proteção de
Dados) caso discorde — https://www.gov.br/anpd

Atenciosamente,
William
```

---

## 4. Tipos de direito (LGPD art. 18) e procedimento

| # | Direito | Procedimento técnico |
|---|---|---|
| I | Confirmação de tratamento | Resposta sim/não com base nas tabelas onde consta o email do titular |
| II | Acesso | Exportar SELECT * (ver 5.1) → enviar JSON |
| III | Correção | UPDATE direto na tabela apropriada após validar identidade |
| IV | Anonimização / bloqueio / eliminação de dados desnecessários | Avaliar caso-a-caso; preferir anonimização (NULL em PII) |
| V | Portabilidade | Mesmo de II, mas em formato estruturado importável |
| VI | Eliminação tratada com consentimento | UPDATE prestadores SET ativo=false + agendar purge 30d |
| VII | Informação sobre compartilhamento | Resposta padrão: Mercado Pago (pagamentos), Resend (email), BrasilAPI (validação CNPJ) |
| VIII | Info sobre não consentir | Resposta padrão informativa |
| IX | Revogação do consentimento | Mesmo de VI quando consentimento for base legal; se for legítimo interesse, ver direito de oposição |

---

## 5. Procedimentos técnicos no banco

### 5.1 Exportação para acesso/portabilidade

Conectar via:
```bash
sudo docker exec -it wins_hub-db-1 psql -U postgres -d wins_hub
```

Query master (ajustar email):
```sql
WITH alvo AS (
  SELECT id FROM prestadores WHERE email = 'titular@email.com'
)
SELECT row_to_json(p) AS prestador FROM prestadores p WHERE id = (SELECT id FROM alvo);

SELECT json_agg(e) AS empresas
FROM empresas_clientes e WHERE e.prestador_id = (SELECT id FROM alvo);

SELECT json_agg(i) AS interacoes
FROM interacoes i WHERE i.prestador_id = (SELECT id FROM alvo);

SELECT json_agg(c) AS contribuicoes
FROM contribuicoes_decisor c WHERE c.confirmado_por = (SELECT id FROM alvo);

SELECT json_agg(l) AS logins
FROM login_logs l WHERE l.email = 'titular@email.com';
```

Salvar saída como `lgpd-acesso-{titular}-{data}.json` e enviar por email.

### 5.2 Exclusão (right to be forgotten)

Soft-delete primeiro, hard-delete em 30d:

```sql
-- Passo 1: desativar imediatamente
UPDATE prestadores
SET ativo = false,
    senha_hash = '__EXCLUIDO_LGPD__',
    senha_temporaria = false,
    excluido_em = NOW(),
    excluido_motivo = 'LGPD-art18-VI'
WHERE email = 'titular@email.com';
```

Passo 2: purga após 30d (rodar via cron mensal):
```sql
UPDATE prestadores
SET nome_empresa = NULL,
    telefone = NULL,
    cnpj = NULL,
    email = 'lgpd-purgado-' || id || '@anonimizado.local'
WHERE excluido_em IS NOT NULL
  AND excluido_em < NOW() - INTERVAL '30 days'
  AND email NOT LIKE 'lgpd-purgado-%';
```

Conservar `id` + `excluido_em` para audit trail (sem PII).

### 5.3 Remoção de decisor (oposição ao legítimo interesse)

Quando um decisor (não-cliente, dado coletado de fonte pública) pede remoção:
```sql
UPDATE decisores_obra
SET excluido_em = NOW(),
    nome = 'REMOVIDO_LGPD',
    cargo = 'REMOVIDO_LGPD',
    email = NULL,
    telefone = NULL,
    linkedin_url = NULL,
    fonte = 'EXCLUIDO_LGPD',
    observacoes = 'Removido a pedido do titular em ' || NOW()::text
WHERE email = 'decisor@email.com';
```

Adicionar a uma blocklist permanente para evitar re-importação:
```sql
-- Tabela proposta (criar quando primeira solicitação chegar):
CREATE TABLE IF NOT EXISTS decisor_lgpd_blocklist (
    email TEXT PRIMARY KEY,
    bloqueado_em TIMESTAMPTZ DEFAULT NOW(),
    motivo TEXT
);
INSERT INTO decisor_lgpd_blocklist (email, motivo)
VALUES ('decisor@email.com', 'LGPD-oposicao-legitimo-interesse');
```
Capturas futuras devem checar essa blocklist no `enriquecer_decisores.py`.

---

## 6. Registro de auditoria (compliance)

Para prova em auditoria ANPD, manter registro de TODA solicitação numa
planilha (Google Sheets ou similar) — colunas mínimas:

| Coluna | Descrição |
|---|---|
| `protocolo` | YYYY-MM-DD-NNN (ex.: 2026-05-09-001) |
| `email_titular` | de quem é o pedido |
| `tipo` | acesso / exclusão / portabilidade / etc |
| `recebido_em` | data/hora do email |
| `confirmacao_enviada_em` | data/hora do email de confirmação (em até 24h) |
| `respondido_em` | data/hora da resposta final (em até 15 dias) |
| `status` | atendido / recusado / pendente |
| `observacoes` | resumo da decisão e justificativa |

Quando volume passar de ~5 solicitações: migrar para tabela `lgpd_solicitacoes`
no banco (Nível B do plano).

---

## 7. Escalação

- **Solicitação juridicamente complexa** (litígio, ANPD, tribunais): consultar advogado antes de responder.
- **Volume incomum** (>3 solicitações em 7 dias): pode indicar coordenação ou problema de imagem — acionar comunicação.
- **Pedido de decisor com email pessoal vinculado a empresa contratante**: tratar com cuidado — pode ser legítimo interesse válido, validar se é figura pública/profissional.

---

## 8. Histórico de revisões

| Data | Mudança |
|---|---|
| 2026-05-09 | Criação inicial (Nível A do plano de operacionalização LGPD) |
