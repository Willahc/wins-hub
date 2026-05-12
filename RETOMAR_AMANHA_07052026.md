# Retomada — Quinta 07/05/2026

**Pausa:** quarta 06/05 às 21:15
**Próximo passo:** quinta 07/05 manhã
**Pitch:** sexta 08/05
**Launch:** segunda 11/05

---

## ✅ O QUE ESTÁ PRONTO

### Backend produto
- 3 estados aplicados: OURO 288 / PRATA 243 / PIPELINE 744
- Endpoints: /api/dashboard/{ouro,prata,pipeline}_count
- Critérios SQL globais no main.py: OURO_DECISOR_SQL, PRATA_MATCH_SQL, PIPELINE_SQL
- Total trabalhável: 1.275 obras / R$ 1,74 trilhão
- Sessão completa: SESSAO_2026_05_06.md

### Pricing P1
- Schema: 10 colunas novas em prestadores (modalidade, ciclo_inicio/fim, periodo_avaliacao_fim, etc)
- Constantes Python: PRECOS_MENSALIDADE (8 combos), DESBLOQUEIOS_INCLUSOS, preco_avulso_por_valor_obra
- Endpoint criar_preferencia aceita modalidade + renunciar_avaliacao
- Webhook MP grava ciclo no prestador
- Sessão completa: SESSAO_PRICING_2026_05_06.md

### Segurança Onda 1
- IP atacante 217.127.68.244 bloqueado (iptables-persistent)
- UFW + fail2ban ativos
- PermitRootLogin no
- User william com SSH key (PowerShell: ssh -i ~/.ssh/winshub_key william@187.127.253.42)
- CORS restrito a winshubcomercial.com.br
- Rate limit 5/min por IP real (X-Forwarded-For)
- .env chmod 600
- CRON_SECRET sem fallback default
- 3 CVEs patchadas (pyjwt 2.10.1, python-multipart 0.0.20, requests 2.32.4)
- Sessão completa: SESSAO_SEGURANCA_ONDA1_2026_05_06.md

### Deck pitch
- PDF de 12 slides gerado
- 8 CSVs em /root/wins_hub/deck_data_dia3.tar.gz (também baixados local em C:\Users\kbadmin\Downloads\deck_data\)
- Slides: Capa / Problema / Solução 3 estados / Jornada / Top10 Ouro / Prata Killer (Ederson Petrobras) / Pipeline TAM / Unit Economics / Modelo / Diferencial / Tração / Roadmap+Quem

---

## 📋 PRÓXIMAS AÇÕES — quinta 07/05

### Manhã (foco no pitch)
1. Abrir PDF do deck e revisar slide a slide
2. Validar todos os números nos slides
3. Marcar slides que precisam mais explicação verbal

### Tarde (ensaio + Onda 2)
4. Ensaiar apresentação 2-3x em voz alta (alvo: 8-10 min)
5. Cronometrar
6. Anotar perguntas que investidor faria

### Pendentes Speedio (se Junior responder)
7. Trial / período de avaliação — escopo
8. Plano 300+ cotação detalhada
9. Tipo de decisor entregue (sócio vs funcional)
10. Cache de consulta vs export
11. Webhook LGPD de exclusão

---

## 📋 SEXTA 08/05 — PITCH
- Deck PDF na mão (online + local backup)
- Mente fresca, hidratado
- Ensaiar slide killer (Ederson) 5x extra na manhã

---

## 📋 SÁB-DOM 09-10/05 — Onda 2 segurança + setup outbound

### Onda 2 segurança (antes de outbound em escala)
- Página /privacidade e /termos publicadas (HTML real, não SPA catch-all)
- Endpoint POST /api/me/excluir-dados (LGPD)
- Headers nginx: CSP, HSTS explícito, Referrer-Policy
- Senha Postgres rotacionada (atual 13 chars, ideal 32)
- Backup offsite (Backblaze B2 ~R$ 5/mês ou similar)
- Verificar bug do path do backup automatizado (cron grava em path diferente dos manuais)
- Disco em 74% — limpar backups antigos
- 11 CVEs restantes (upgrade FastAPI/uvicorn — testar antes)

### Setup outbound (gratuito por enquanto)
- Resend free 3k/mês já configurado
- Tabelas outbound_campanhas + outbound_envios
- Endpoint /unsubscribe?token= (opt-out obrigatório legal)
- Pixel de tracking de open
- Templates A/B email cold (factual vs curioso)
- Recorte inicial: CNAE 4321500 (instalação elétrica) + UF SP + porte ME/EPP + email corporativo próprio
- Universo total disponível: 2.659.080 empresas / 483k com email corporativo / 320k apenas no CNAE 4321500

---

## 📋 SEGUNDA 11/05 — LAUNCH

- Card Pipeline no frontend (B4 que ficou pendente do dia 3)
- Validação visual completa pelo William (login GRATUITO/STANDARD/PREMIUM)
- Primeira campanha outbound: 30 emails/dia
- Ramp-up gradual ao longo da semana

---

## 📋 PÓS-LAUNCH (semana 2-3)

### Pricing fase 2 (regra dos 7 dias)
- B4: regra 7 dias no /desbloquear (decidir bloqueio total vs só inclusos)
- B6: cron de liberação de créditos no dia 8
- B7: frontend /planos com toggle modalidade + checkbox renúncia
- Fix bugs MP: downgrade_silencioso, signup_plano_inicial

### Speedio integrada
- Tabelas decisor_cache + desbloqueios
- Endpoint /api/obras/{id}/desbloquear-decisor com lock distribuído
- TTL 90 dias + revalidação
- LGPD: webhook de exclusão Speedio + polling

---

## 🔑 CREDENCIAIS / ACESSOS

- VPS: 187.127.253.42 (root@srv1617037)
- SSH agora: ssh -i ~/.ssh/winshub_key william@187.127.253.42 (PowerShell Windows)
- Postgres: container wins_hub-db-1
- Backups: /root/backups/wins_hub/

---

## 🚨 ATAQUES MITIGADOS HOJE

- Brute-force SSH ativo de 217.127.68.244 (10/seg) — BLOQUEADO
- 543 tentativas falhadas no log — fail2ban agora protege
- IP legítimo do William: 177.76.18.24
