# Onda 1 — Correções de Segurança Críticas
**Data:** 2026-05-06 ~24:00 BRT · **Status:** S1-S2-S4-S5-S6-S7-S8 aplicados; S3 PARCIAL aguardando confirmação

## Aplicadas com sucesso

| # | Item | Status | Notas |
|---|---|---|---|
| **S1** | IP atacante 217.127.68.244 bloqueado via iptables | ✅ | `iptables-save` em `/etc/iptables/rules.v4`. ⚠️ Sem `netfilter-persistent` instalado, regra some no reboot — pendência |
| **S2 / UFW** | UFW instalado, regras 22+80+443 (+ 3001 legado), `--force enable` | ✅ | `DEFAULT_FORWARD_POLICY=ACCEPT` ajustado em `/etc/default/ufw` ANTES do enable, pra preservar Docker forwarding |
| **S2 / fail2ban** | fail2ban ativo com jail sshd | ✅ | `systemctl is-active fail2ban` = active. 0 banidos no momento |
| **S4** | CORS restrito a `winshubcomercial.com.br` (+ www, localhost dev) | ✅ | Verificado: origem maliciosa não recebe `access-control-allow-origin`; origem autorizada recebe. `allow_methods=["*"]` e `allow_headers=["*"]` mantidos |
| **S5** | Rate limit via `slowapi`: login 5/min, registro 3/min | ✅ | Confirmado: 6ª tentativa de login retornou HTTP **429**. `slowapi==0.1.9` no requirements |
| **S6** | `CRON_SECRET` sem fallback default `"secret"` — agora `os.environ["CRON_SECRET"]` strict | ✅ | `.env` já tinha `CRON_SECRET` (32 hex chars) |
| **S7** | Bumps conservadores de pacotes com CVE | ✅ parcial | pyjwt 2.8.0→2.10.1, python-multipart 0.0.9→0.0.20, requests 2.32.3→2.32.4. Corrigiu 2 CVEs confirmados (CVE-2024-47081, CVE-2024-53981); restam 5 CVEs CVE-2026-* + 2 starlette + 4 pip — exigem upgrades mais arriscados |
| **S8** | `.env` chmod **600** (era 644 world-readable) | ✅ | `-rw------- root:root` |

## S3 — PARCIAL, aguarda confirmação manual

| Sub-item | Status |
|---|---|
| User `william` criado com `sudo` group | ✅ |
| `sudoers.d/william` com `NOPASSWD:ALL` (durante setup) | ✅ |
| Senha temporária gerada | ✅ Anotada no terminal: `b0837f4876b8759186cdfb58` |
| `/home/william/.ssh/authorized_keys` populado | ⚠️ **Vazio — mesma situação do root** |
| `PermitRootLogin no` em sshd_config | ❌ **NÃO aplicado** |

### ⚠️ Bloqueador descoberto sobre SSH

`/root/.ssh/authorized_keys` está **vazio (0 linhas)** — você loga como root via senha, não chave. Por isso william também não tem chave para copiar. Se desativar `PermitRootLogin no` agora, você fica preso fora se a senha do william for esquecida (e sem chave SSH).

### Antes de desativar root SSH (decisão sua, próxima sessão):

1. **Recomendado**: Adiciona sua chave pública (id_rsa.pub do seu notebook) em `/home/william/.ssh/authorized_keys`, depois testa `ssh -i ~/.ssh/id_rsa william@187.127.253.42` em **outra janela**.
2. Quando confirmar acesso por chave pelo william, executa: `sed -i 's/^PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config && systemctl reload sshd`
3. Considera também trocar `PasswordAuthentication yes` (default) → `no` se for usar só chave.

## Smoke tests verificados

| Teste | Resultado |
|---|---|
| `https://winshubcomercial.com.br/` | HTTP 200 (108ms) |
| `/api/dashboard/ouro_count` | HTTP 200, 288 |
| `/api/dashboard/prata_count` | HTTP 200, 243 |
| `/api/obras?limit=5` | HTTP 200 |
| `/login` | HTTP 200 |
| Rate limit login (6ª tentativa) | HTTP **429** ✅ |
| CORS origem maliciosa | sem `access-control-allow-origin` ✅ |
| CORS origem autorizada | `access-control-allow-origin: https://winshubcomercial.com.br` ✅ |
| API logs após restart | sem erros, 2 workers up |
| pip-audit pós | 11 CVEs restantes (era 13) |

## Pendências documentadas

### Crítico, próxima sessão
1. **William** adiciona chave SSH pública no `/home/william/.ssh/authorized_keys`, testa login por chave, então desativa `PermitRootLogin yes`
2. **Rate limit por IP real**: hoje slowapi vê `request.client.host` que com nginx sempre é `127.0.0.1` — limita 5/min globalmente em vez de por usuário. Configurar `key_func` que lê `X-Forwarded-For` quando trusted (precisa nginx passar header `X-Real-IP` ou similar — verificar se já passa)
3. **iptables persistence**: instalar `netfilter-persistent` e `iptables-persistent` para regra do IP atacante sobreviver a reboot

### Médio (após launch)
4. **CVEs restantes (11)**: pip (4 — só dev, baixo risco), pyjwt 2.10.1→2.12.0 (CVE-2026-32597), python-multipart 0.0.20→0.0.27 (3 CVEs CVE-2026), requests→2.33.0 (CVE-2026-25645), starlette→0.40+ (precisa testar compat com FastAPI 0.111.0 ou bumpar FastAPI para 0.115+)
5. **CSP / HSTS / Referrer-Policy** em nginx (já tem X-Frame e X-Content-Type)
6. **Backup offsite** (S3/Backblaze)
7. **Política de Privacidade + Termos + LGPD endpoint** (legal antes de outbound em escala)

### Baixo
8. **Sentry / error tracking**
9. **Uptime monitor externo**

## Backups (todos preservados em /root/backups/wins_hub/ e /root/wins_hub/*)

- DB: `wins_hub_pre_seguranca_20260506_2352.sql` (1.3 GB)
- main.py: `app/main.py.bak_pre_seguranca_20260506_2352` (125 KB)
- nginx: `nginx/conf.d.bak_pre_seguranca_20260506_2352/`
- sshd_config: `/etc/ssh/sshd_config.bak_pre_seguranca_20260506_2352`
- .env: `.env.bak_pre_seguranca_20260506_2352`

## Mudanças no main.py (resumo)

- `slowapi` imports + `Limiter(key_func=get_remote_address)` 
- `app.state.limiter = limiter` + `add_exception_handler(RateLimitExceeded, ...)`
- `@limiter.limit("5/minute")` em `/api/auth/login`, agora com `request: Request` como primeiro parâmetro
- `@limiter.limit("3/minute")` em `/api/auth/registro`, idem
- `CORSMiddleware allow_origins` agora restrito a 4 origens
- `CRON_SECRET` agora `os.environ["CRON_SECRET"]` (sem fallback)

## Mudanças no requirements.txt

- adicionado: `slowapi==0.1.9`
- bumped: `pyjwt 2.8.0→2.10.1`, `python-multipart 0.0.9→0.0.20`, `requests 2.32.3→2.32.4`

API rebuild + restart concluídos sem erros.
