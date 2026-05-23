# Skill: Paywall Decisor — Regra Canônica

## Níveis de acesso

| Campo decisor     | Anônimo | Cadastrado GRATUITO | Assinante ESSENCIAL+ |
|-------------------|---------|---------------------|----------------------|
| Quantidade        | ✅      | ✅                  | ✅                   |
| Resumo (contagens)| ✅      | ✅                  | ✅                   |
| Nome              | 🔒      | 🔒                  | ✅                   |
| LinkedIn          | 🔒      | 🔒                  | ✅                   |
| Email             | 🔒      | 🔒                  | ✅                   |
| Telefone decisor  | 🔒      | 🔒                  | ✅                   |
| Telefone empresa  | 🔒      | 🔒                  | ✅                   |

## Resumo público (sempre visível)
```json
"decisores_resumo": {
  "total": 3,
  "com_linkedin": 2,
  "com_email": 1,
  "com_telefone_decisor": 1,
  "com_telefone_empresa": 1
}
```
Nunca expor dados reais — só contagens.

## Modal
Qualquer clique em campo bloqueado → modal "Assine para desbloquear".

## Regra geral
NUNCA expor nome, LinkedIn, email ou telefone de decisor sem verificação de plano.
