# Governança de Skills do WiNS Hub

**Status:** aceita
**Data:** 2026-05-10
**Autor:** William (decisão); Claude Code (registro)
**Próxima revisão:** 2026-08-10 (trimestral)

---

## Contexto

O WiNS Hub opera com sessões longas de Claude Code, frequentemente
multi-fase, em produção. Em 09-10/05/2026 três incidentes consecutivos
ensinaram que padrões repetidos sem codificação levam a esquecimento,
inconsistência e regressão silenciosa:

1. **09/05 — cleanup CNPJ órfãs.** UPDATE em massa zerou ~250 CNPJs em
   7 fontes; trigger de auditoria não monitorava o campo afetado, então
   a mudança foi invisível. Detectada só em 10/05 por acidente.
2. **10/05 — faxina disco fase 1.** Liberou 4.8 GB sem perda graças a
   manifesto + dry-run, mas o processo foi reinventado do zero a cada bloco.
3. **10/05 — antt_rod recovery.** Pipeline de R$ 19 bi recuperado via
   Playwright + BrasilAPI. Padrão de "dry-run de transação" (`BEGIN; UPDATE;
   ROLLBACK` antes do `COMMIT` real) emergiu organicamente.

Os três incidentes têm padrões comuns: estrutura B1..BN, manifesto pré-mudança,
validação antes/depois, status no banco. Sem skills, cada sessão reaplica
esses padrões manualmente — alguns serão esquecidos.

## Decisão

Adotar **skills do Claude Code** como mecanismo primário de codificação de
padrões operacionais do WiNS Hub. Skills moram em
`/root/wins_hub/.claude/skills/<nome>/SKILL.md`, são carregadas
automaticamente pelo Claude Code quando o gatilho da `description` casa
com o pedido do usuário.

## Skills criadas hoje (10/05/2026)

| Skill | Função | Linhas |
|-------|--------|------:|
| [wnshub-mega-briefing](../../.claude/skills/wnshub-mega-briefing/SKILL.md) | Estrutura B1..BN, manifestos, pontos de parada | 193 |
| [wnshub-sql-producao](../../.claude/skills/wnshub-sql-producao/SKILL.md) | Protocolo BEGIN/dry-run/COMMIT em transação | 260 |
| [wnshub-anti-alucinacao](../../.claude/skills/wnshub-anti-alucinacao/SKILL.md) | Validação de dado externo (CNPJ, email, etc) com fonte+DV+cross-check | 233 |

Total: 686 linhas. Cada skill <500 (limite recomendado pelo skill-creator).

## Por que skills, e não memórias longas ou documentação

| Mecanismo | Custo de criar | Cobertura | Risco de esquecer |
|-----------|---------------|-----------|-------------------|
| Memória curta (MEMORY.md entry) | baixo | recupera contexto, não ensina protocolo | médio — entry pode ser truncado |
| Documentação em `/docs/` | médio | só ajuda se humano ler | alto — humanos não relêem |
| **Skill** | médio (uma vez) | **carregada automaticamente** quando gatilho casa | baixo — sistema garante |
| CLAUDE.md (instruções globais) | alto (afeta todo o projeto) | sempre carregado | baixo, mas peso fixo no contexto |

Skills ocupam contexto **só quando relevantes**, então cobrem muito mais
padrões que CLAUDE.md sem inchar.

## Estrutura padrão de uma skill WNS Hub

```
.claude/skills/wnshub-<nome>/
├── SKILL.md            (obrigatório — YAML frontmatter + corpo Markdown)
├── scripts/            (opcional — scripts executáveis bundleados)
├── references/         (opcional — docs auxiliares carregados sob demanda)
└── assets/             (opcional — templates, fixtures)
```

Convenções:

- **Prefixo `wnshub-`** em todas as skills do projeto. Distingue de skills
  oficiais do marketplace (`skill-creator`, `frontend-design`, etc.) e
  evita colisão futura.
- **`description` na frontmatter é pushy mas precisa.** Diz o que faz E
  quando disparar, com gatilhos concretos. O skill-creator alerta que
  Claude tende a "undertriggar" — descrições muito tímidas falham.
- **`<500 linhas` no corpo.** Se aproximar, criar subdir `references/` e
  fragmentar.
- **Linkar incidentes consagrados.** Cada skill cita o incidente que a
  motivou (com data) para que a regra tenha causa-raiz auditável.
- **Imperativo, com "why".** Imperativos curtos ("rode X", "valide Y")
  intercalados com explicação do motivo. Evitar `ALWAYS`/`NEVER` em
  caixa-alta — preferir explicar.

## Processo de criação de novas skills

Uma skill nasce de **um destes três gatilhos**:

1. **Incidente que custou tempo/dinheiro/confiança** — vira skill assim que
   a poeira baixar. Exemplo: 09/05 -> `wnshub-anti-alucinacao`.
2. **Padrão repetido 3+ vezes em sessões diferentes** — quando o operador
   percebe "isso já fizemos antes do mesmo jeito", codifica.
3. **Briefing explícito do William** ("crie skill X") — caso desta sessão.

Nascimento por inspiração isolada (sem incidente nem repetição) é
desencorajado: skills podem ficar dormentes e poluir context window se mal
calibradas.

## Manutenção e revisão trimestral

**Próxima revisão: 2026-08-10** (3 meses).

Checklist da revisão:

- [ ] Cada skill foi efetivamente carregada em alguma sessão? (Se nenhuma
      sessão disparou em 90 dias, reavaliar gatilho ou aposentar.)
- [ ] As lições aprendidas continuam relevantes? (Trigger de auditoria
      em `obras` foi corrigido? Se sim, atualizar texto da skill.)
- [ ] Há novos incidentes em `/root/wins_hub/docs/incidentes/` que pedem
      skill própria?
- [ ] Há padrão em ascensão que merece virar skill (rule de 3)?
- [ ] Alguma skill cresceu além de 500 linhas? Quebrar em references/.

## Skills futuras candidatas (backlog)

Estas estão em standby até gatilho real:

- **wnshub-pesquisa-dominios** (semana 2 maio) — padrão de busca de domínio
  oficial de empresa via WHOIS + LinkedIn + cross-check com `empresa_intel`.
  Gatilho: padrão se repete em 3+ obras Ouro Parcial.
- **wnshub-investigacao-forense** (semana 2 maio) — protocolo F1..F8 do
  diagnóstico de regressão silenciosa. Gatilho: próxima regressão
  inexplicada no banco. Material rascunho já existe no manifesto do
  incidente 09/05.
- **wnshub-outbound-protocol** (pós-launch, ~25/05) — quando outbound
  comercial começar, padrão de "primeira mensagem + follow-up + log no CRM"
  pode merecer codificação. Aguarda dados reais de o que funciona.

Nenhuma delas é criada hoje — esperar gatilho concreto evita over-engineering.

## Como o usuário sabe que uma skill foi carregada

Skills carregadas aparecem na lista de `available-skills` que o Claude Code
envia ao iniciar cada turno. Se o usuário quer auditar:

```bash
ls /root/wins_hub/.claude/skills/
```

Se o usuário sente que a skill que devia ter carregado **não carregou**, o
problema está na `description` (gatilho fraco). Solução: editar a
`description` pra ser mais explícita sobre os contextos.

## Anti-patterns conhecidos para skills WNS Hub

1. **Skill que tenta cobrir tudo.** Uma skill = um protocolo. Se há dois
   protocolos relacionados, são duas skills com cross-reference.
2. **`description` genérica.** "Para tarefas SQL" carrega em demais
   contextos. Específico: "para mutação SQL em produção".
3. **Skill sem "why".** Vira lista de imperativos secos que Claude executa
   mecanicamente. Cada regra precisa de motivação.
4. **Mover skills entre repositórios mas esquecer de atualizar paths.**
   `.claude/skills/` é por-projeto. Skills genéricas, se útil, podem
   migrar pra `~/.claude/skills/` (usuário-global).
5. **Sobreposição agressiva com CLAUDE.md.** CLAUDE.md descreve o projeto;
   skills descrevem **como operar** em situações específicas. Não duplicar.

## Versionamento

Skills não têm versão explícita no nome. Mudanças no SKILL.md são
registradas via git (quando o projeto for versionado) ou via cópia
`.bak_pre_<contexto>_YYYYMMDD_HHMMSS` antes de edição grande. Convenção:

- Edits pequenos (typo, exemplo extra): sem backup, vai direto.
- Edits que mudam protocolo: backup antes, manifesto em
  `/root/.faxina_log/skill_<nome>_<data>.txt`.

## Custo de contexto

Cada skill carregada consome ~200-500 linhas de contexto enquanto está
ativa. As 3 atuais somam ~686 linhas (~10K tokens). Aceitável.

Limite operacional: até **5 skills WNS Hub ativas simultaneamente** (cap
auto-imposto). Acima disso, o context window fica pressionado e o
trade-off muda. Se for necessário ir além, é sinal pra dividir skills
grandes ou consolidar redundância.

## Referência

- Skill-creator oficial: `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/skill-creator/skills/skill-creator/SKILL.md`
- Manifesto desta criação: `/root/.faxina_log/skills_criacao_20260510.txt`
- Memória de projeto: `/root/.claude/projects/-root-wins-hub/memory/skills_wnshub.md`
