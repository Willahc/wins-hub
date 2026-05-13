# Playbook Mari — Outbound Top ICP WiNS Hub

> Cadência outbound 5 dias pra os fornecedores top do ICP enriquecido com decisor.
> Versão 1.0 · 2026-05-13

## Como ler o CSV (`mari_icp_decisores_<data>.csv`)

| Coluna                  | O que é                                                  |
| ----------------------- | -------------------------------------------------------- |
| `cnpj`                  | CNPJ raiz (14 dígitos, sem máscara)                      |
| `razao_social`          | Nome oficial RFB                                         |
| `cnae_principal`        | CNAE 7-dígitos sem hífen (ex: 7112000 = engenharia)      |
| `porte`                 | DEMAIS / EPP                                             |
| `uf` / `municipio`      | Localização da matriz                                    |
| `tel_empresa`           | Telefone Receita (pode estar desatualizado)              |
| `email_empresa`         | Email Receita (genérico, baixa qualidade)                |
| `decisor_nome`          | Decisor identificado via P3.1 (LinkedIn/CREA/DOU)        |
| `decisor_cargo`         | Cargo do decisor                                         |
| `decisor_email`         | **Email do decisor** (Hunter ou pattern)                 |
| `decisor_telefone`      | Telefone direto (raro)                                   |
| `linkedin_url`          | URL LinkedIn profissional                                |
| `matches_ouro`          | Quantas obras Ouro o fornecedor matchou                  |
| `capex_total_ouro`      | Soma do CAPEX das obras Ouro (R$)                        |
| `score_prioridade`      | matches_ouro × 10 + matches_prata × 3                    |

**Filtro recomendado**: começar por `decisor_email != ''` + ordenar por `score_prioridade DESC`.

## Cadência 5 dias

### Dia 1 — LinkedIn (pre-aquecimento)

- Abra `linkedin_url` do decisor.
- Verifique nas últimas 14 dias se tem postagem (atividade = sinal de receptividade).
- **Enviar convite de conexão** com nota curta (300 chars máx):

```
Olá, [Primeiro Nome]! Vi que [Empresa] foi mapeada em obras de [setor: energia/saneamento/infra] na nossa base do WiNS Hub. Posso compartilhar 1 oportunidade específica relacionada à [UF]? Sem compromisso.
```

> Não conectar se: foto/perfil suspeito, último post > 12 meses, ou cargo já não está alinhado (LinkedIn mostra "Open to Work" sem o cargo do CSV).

### Dia 2 — Email 1 (descobrir interesse)

Subject: **`Obra [Setor] em [UF] — vale 5 min?`**

```
Olá, [Primeiro Nome],

Trabalho com inteligência de obras industriais e mapeamos [N] obras Ouro 
em [UF] onde [Empresa] tem CNAE compatível ([cnae_principal]).

Uma delas: [Nome da obra top]. CAPEX estimado: R$ [valor].

Posso te enviar o decisor responsável dessa obra + briefing técnico em 1 página?

Se útil, agendamos 15 min essa semana.

[Sua assinatura]

PS: Para uma visão completa das [matches_ouro] obras matched, abra:
[link app]/obra/{primeira_obra_ouro_id}
```

### Dia 3 — Sem ação (deixar email decantar)

### Dia 4 — Call telefônica (se `decisor_telefone` ou `tel_empresa`)

Script (60s):
```
Bom dia, posso falar com [Primeiro Nome]?
Sou da WiNS Hub, mapeamos obras industriais e identificamos [N] em [UF]
compatíveis com o portfólio da [Empresa]. Já enviei email mas quis falar
diretamente. Tem 5 min essa semana pra eu mostrar 2-3 oportunidades?
```

### Dia 5 — Follow-up email (último toque)

Subject: **`Re: Obra [Setor] em [UF] — última tentativa`**

```
[Primeiro Nome],

Sem novidades — entendi que talvez não seja o momento.

Caso queira voltar a esse contato no futuro, basta responder. Mantemos o
mapa de obras atualizado diariamente; quando aparecer algo realmente
encaixado pra [Empresa], te aviso direto.

Boa semana,
[Assinatura]
```

### Dia 7+ — Breakup

Email de saída suave: `"Vou pausar contato. Se mudar algo, é só falar."`

## Métricas a registrar (em planilha lado-a-lado)

Para cada CNPJ:

| Campo                       | Tipo            |
| --------------------------- | --------------- |
| `linkedin_conectou`         | Sim/Não/Pendente|
| `linkedin_aceitou_data`     | data            |
| `email_1_enviado`           | data            |
| `email_1_aberto`            | Sim/Não/?       |
| `email_1_respondido`        | Sim/Não         |
| `call_data`                 | data            |
| `call_quem_atendeu`         | "Decisor" / "Secretária" / "Não atendeu" |
| `call_duracao_min`          | int             |
| `email_2_enviado`           | data            |
| `email_2_respondido`        | Sim/Não         |
| `resposta_classificacao`    | "interessado" / "depois" / "nao_obrigado" / "sem_resposta" |
| `reunido_data`              | data            |
| `pipeline_atual`            | "lead" / "discovery" / "proposta" / "ganho" / "perdido" |

Meta: registrar pelo menos `email_1_enviado` + `resposta_classificacao` pra alimentar análise post-mortem.

## Targets primeira semana

- 200 CNPJs enviado convite LinkedIn (Dia 1)
- 150 emails enviados (Dia 2)
- 50 calls tentadas (Dia 4)
- **30 respostas / 15 reuniões marcadas** (meta agressiva)

## O que NÃO fazer

- ❌ Mandar email genérico em massa sem citar a obra específica.
- ❌ Contactar decisor sem ter aberto o LinkedIn antes (cargo pode ter mudado).
- ❌ Empurrar a venda sem mostrar a obra como gancho de relevância.
- ❌ Não registrar a interação — perdemos o aprendizado pra próximo batch.

## Onde puxar dados frescos

Pra cada decisor, o app WiNS Hub tem:
- Página `/obra/{id}` mostra o decisor curado + cache P3.1 + CRM
- Endpoint `/api/obras/{id}/detalhe` retorna decisor mascarado se não desbloqueou
- Hunter quota residual visível no dashboard admin

Em caso de email com bounce ou LinkedIn que mudou, basta abrir `/obra/{primeira_obra_ouro}` → botão "Buscar Decisor" re-executa P3.1 (custa R$10 wallet, ou grátis se admin).
