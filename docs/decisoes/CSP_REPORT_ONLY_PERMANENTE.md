# CSP em Report-Only permanente até gatilho de revisão

**Status:** aceita  
**Data:** 2026-05-10  
**Autor:** William (decisao); Claude Code (registro tecnico)  
**Proxima revisao:** 2026-08-10 ou em qualquer gatilho listado abaixo

---

## Contexto

A policy `Content-Security-Policy-Report-Only` esta ativa no nginx desde
antes de 2026-05-10 (data exata pre-existente nao registrada). O endpoint
`POST /api/csp-report` foi adicionado em 2026-05-10 12:25 UTC para coletar
violacoes em `/var/log/wins_hub/csp-violations.jsonl` (rotacao semanal via
logrotate).

Validacao via Chromium headless (Playwright) em 2026-05-10 navegou pelas 5
paginas principais (`/`, `/login`, `/score`, `/como-funciona`, `/vendas`)
e identificou **uma unica violacao recorrente**:

```
script-src | eval | source=https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js
```

Causa raiz: Alpine.js 3.x usa `new Function("...")` em runtime para compilar
expressoes em atributos `x-show`, `@click`, `x-bind:class`, etc. Para o CSP,
`new Function()` e equivalente a `eval()` e exige `'unsafe-eval'` em
`script-src`.

Consequencia: enforcar a policy atual quebra o Alpine e portanto a SPA
inteira (botoes nao respondem, modais nao abrem, gate de plano nao avalia).

## Opcoes consideradas

| Caminho | Esforco | CSP fica... | Decisao |
|---------|--------:|-------------|:-:|
| A. Enforce com `'unsafe-eval'` adicionado | 1 linha de nginx + reload | Simbolico (script-src com `'unsafe-inline'` + `'unsafe-eval'`); outras diretivas ativas | descartado (ver "Sobre opcao A" abaixo) |
| B. Migrar pra `@alpinejs/csp` build (sem eval) | 4-8h refator de ~1760 linhas Alpine inline em index.html | Defesa profunda real | adiado (gatilho 3) |
| C. Manter Report-Only com instrumentacao | 0 alem do que ja foi feito | Telemetria sem bloqueio | **aceito** |

## Decisao

Manter `Content-Security-Policy-Report-Only` indefinidamente, com
instrumentacao ativa em `/api/csp-report`. Reavaliar so em gatilhos
explicitos (ver abaixo).

Defesa em camadas continua sendo provida por:

- **HTTPS + HSTS** (`max-age=31536000; includeSubDomains; preload`) — TLS forte
- **JWT** com rotacao via /api/auth/refresh
- **Rate limit** via slowapi nas rotas sensiveis (login, registro, etc)
- **CORS restrito** (apenas `winshubcomercial.com.br` + localhost dev)
- **SSH key only** (sem password)
- **`X-Frame-Options: DENY`** — protege contra clickjacking
- **`X-Content-Type-Options: nosniff`**
- **`Referrer-Policy: strict-origin-when-cross-origin`**
- **`Permissions-Policy`** restritiva (geo/mic/camera/payment desabilitados)

## Sobre a afirmacao "frame-ancestors / form-action / base-uri sao enforced em Report-Only"

**A afirmacao do briefing original esta INCORRETA.** Documento aqui pra
nao perpetuar o erro.

### Por que esta errada

Pela especificacao W3C CSP3, secao 2.3.2:

> A policy that is delivered via the Content-Security-Policy-Report-Only
> header field MUST NOT enforce the policy. (...) The user agent MUST
> monitor the policy and SHOULD report violations, but MUST NOT prevent
> the execution of any otherwise-legal action.

E na MDN:

> The HTTP Content-Security-Policy-Report-Only response header allows web
> developers to experiment with policies by monitoring (but not enforcing)
> their effects.

O nome do header e literal: **Report-Only nao enforca NENHUMA diretiva**,
incluindo `frame-ancestors`, `form-action` e `base-uri`. Todas sao apenas
reportadas.

### Validacao empirica

A propria evidencia do nosso teste de 2026-05-10 confirma:

- 499 violacoes de `eval` foram REPORTADAS pelo Chromium na home
- O Alpine.js continuou funcionando normalmente (Playwright leu titulos,
  conseguiu navegar entre paginas)
- **Se Report-Only enforcasse, Alpine teria parado** — porque `eval` esta
  na mesma policy que `frame-ancestors`/`form-action`/`base-uri`

Logo, em modo Report-Only, **NENHUMA das diretivas restritivas esta
bloqueando nada agora**.

### Implicacao defensiva

| Diretiva | Em Report-Only enforca? | Tem alternativa ativa hoje? |
|----------|:-:|---|
| `frame-ancestors 'none'` | ❌ nao | ✅ **sim** — `X-Frame-Options: DENY` (ativo no nginx) |
| `form-action 'self'` | ❌ nao | ❌ **nao** ha header alternativo |
| `base-uri 'self'` | ❌ nao | ❌ **nao** ha header alternativo |
| `default-src 'self'`, `script-src`, `style-src`, `font-src`, `img-src`, `connect-src` | ❌ nao | parcial (CORS restrito cobre `connect-src` em parte) |

Resultado: clickjacking protegido por `X-Frame-Options`. As demais
diretivas sao puramente telemetria. Aceitavel para o estagio do produto
(pre-launch), com gatilhos definidos para reavaliar.

## Sobre a opcao A

A opcao A (enforcar com `'unsafe-eval'` + `'unsafe-inline'`) ativaria as
diretivas `form-action` e `base-uri` (que hoje sao so telemetria). Mas
analisando custo-beneficio:

- **Risco de regressao:** mudar `Report-Only` -> enforce e mudanca
  no path de runtime. Qualquer recurso ainda nao mapeado (ex: integracao
  futura com Mercado Pago checkout, Hotjar, Cloudflare Turnstile) quebra
  o site sem avisar.
- **Beneficio defensivo marginal:** `form-action` e `base-uri` cobrem
  vetores de ataque que dependem de injecao HTML — que requer XSS
  primeiro. Como ja temos `'unsafe-inline'` permitido, XSS via inline
  nao e bloqueado mesmo em enforce.
- **Custo de complexidade:** mais um ponto de falha em produto solo.

Decisao: NAO aplicar opcao A. Telemetria + camadas existentes >
enforce simbolico.

## Gatilhos para reavaliar

Reavaliar a decisao QUANDO QUALQUER UM dos gatilhos abaixo for atingido:

1. **>500 usuarios ativos/dia** — superficie de risco aumenta materialmente
2. **Primeiro contrato enterprise** — auditoria externa de seguranca
   provavelmente exige enforce
3. **Proxima reescrita major do frontend** — janela natural pra migrar
   pra `@alpinejs/csp` build (caminho B) e ganhar defesa profunda real
4. **Manuseio de PII sensivel de compradores** (CPF, dados bancarios,
   contratos digitalizados) — exige defesa profunda

## Quem revisa

William, em revisao trimestral de seguranca.

## Proxima revisao agendada

**2026-08-10** (90 dias).

## Telemetria contínua

Cron mensal valida que nao apareceram violacoes novas (recursos externos
nao previstos). Detalhe em `/root/wins_hub/scripts/validate_csp.py` e
linha de cron correspondente.

## Referencias

- W3C CSP Level 3: https://www.w3.org/TR/CSP3/
- MDN CSP-Report-Only: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy-Report-Only
- Manifesto da implementacao: `/root/.faxina_log/csp_caminho_a_20260510.txt`
- Validacao Playwright: `/root/wins_hub/scripts/validate_csp.py`
- Log de violacoes: `/var/log/wins_hub/csp-violations.jsonl`
