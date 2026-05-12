# Fase A3 — Pendentes (6 concessionarias) — manifesto pre-mudanca

**Data:** 2026-05-10 (tarde)
**Operador:** Claude Code (sessao williamvnvn@gmail.com)
**Autorizacao:** William, "Autorizacao PR2 - prossegue"
**Skills aplicadas:** `wnshub-mega-briefing`, `wnshub-sql-producao`, `wnshub-anti-alucinacao`

## Contexto

Fase A3 da manha (10/05) recuperou CNPJ de 20 das 26 obras antt_rod. As 6
pendentes foram para revisao/falha por DDG retornar homonimos comerciais
(VIA SUL, VIABRASIL), ausencia de hits (ECO RIOMINAS, VIA BAHIA), homonimo
generico (CRO) ou situacao BAIXADA (VIA 040). William buscou manualmente
via web 4 CNPJs validados; sessao continuou.

### Validacoes pre-UPDATE concluidas

- **V1 (schema)**: colunas `cnpj_pendente_validacao` e `updated_at` solicitadas
  no briefing original NAO EXISTEM em `obras`. Decisao: omitir e usar apenas
  o que existe (`cnpj_status`, `validacao_metodo`, `validacao_data`,
  `observacoes_validacao`). Mesma decisao da fase A3 da manha.
- **V2 (VIA BAHIA)**: cenario 1 confirmado com 5 evidencias independentes
  (endereco, data, CNAE, socio espanhol, comunicado oficial gov.br).
  VIABAHIA virou CONCROD pos-encerramento da concessao (15/05/2025),
  CNPJ 10.670.314/0001-55 inalterado.
- **V3 (CRO vs VIA SUL)**: matematicamente NAO E duplicacao (soma 2023-2024
  bate exatamente com valor_estimado de cada uma no banco, R$1.674bi e
  R$1.427bi respectivamente). Porem CRO permanece misterio — investimentos
  crescentes em 2024 (R$ 1.207bi) incompativeis com Concessionaria
  Osorio-PoA encerrada em 2017. Sem evidencia primaria, NAO atribuir CNPJ.

## Diff de filtro WHERE

Briefing pediu `WHERE empresa IN ('VIA SUL', ...)`. Substituido por
`WHERE id_externo IN ('ANTT-ROD-VIA_SUL', ...)` porque a coluna `empresa`
no banco tem nomes longos (`'VIA SUL - CONCESSIONARIA DE RODOVIAS S.A.'`)
e o IN com sigla curta resulta em 0 linhas afetadas. Mesmo padrao seguro
da fase A3 da manha.

## 6 UPDATEs propostos

### Grupo 1 — 3 obras com CNPJ + validacao padrao
| id_externo | empresa | cnpj_novo | razao_social BrasilAPI | UF |
|------------|---------|-----------|------------------------|----|
| ANTT-ROD-VIA_SUL | VIA SUL - CONCESSIONARIA DE RODOVIAS S.A. | 32161500000100 | CONCESSIONARIA DAS RODOVIAS INTEGRADAS DO SUL S.A. | RS |
| ANTT-ROD-VIABRASIL | VIABRASIL - CONCESSIONARIA DA BR-116/376/101 S.A. | 44067725000172 | VIA BRASIL BR 163 CONCESSIONARIA DE RODOVIAS S.A. | MT |
| ANTT-ROD-ECO_RIOMINAS | ECO RIOMINAS S.A. | 29884545000190 | ECORIOMINAS CONCESSIONARIA DE RODOVIAS S.A. | RJ |

Todas: situacao=ATIVA, CNAE=5221, DV valido.
Set: `cnpj`, `cnpj_status='validated_manual_brasilapi'`, `validacao_metodo='manual_brasilapi_websearch'`, `validacao_data=CURRENT_DATE`.

### Grupo 2 — VIA BAHIA com observacoes especial
- id_externo: ANTT-ROD-VIA_BAHIA
- cnpj: 10670314000155 (DV valido)
- razao_social atual BrasilAPI: CONCROD CONCESSIONARIA DE RODOVIAS LTDA.
- Observacao registra continuidade juridica VIABAHIA -> CONCROD pos-encerramento concessao 15/05/2025

### Grupo 3 — CRO com observacoes investigativa (SEM CNPJ)
- id_externo: ANTT-ROD-CRO
- Permanece cnpj=NULL
- `cnpj_status='pending_investigation'`
- Observacao explica achado misterioso V3 + proximo passo

### Grupo 4 — VIA 040 marcacao concessao revogada (SEM CNPJ)
- id_externo: ANTT-ROD-VIA_040
- Permanece cnpj=NULL
- `cnpj_status='concessao_revogada'`
- `validacao_metodo='manual_documental'`
- Observacao registra encerramento 06/08/2024 e referencia a EPR Via Mineira (sucessora no trecho BH-JF)

## SQL completo

Vide arquivos:
- `/tmp/a3_pendentes_dryrun.sql` (BEGIN; ...; ROLLBACK; — desta etapa)
- `/tmp/a3_pendentes_commit.sql` (BEGIN; ...; COMMIT; — apos autorizacao)

## Criterio de sucesso pos-COMMIT (B7.3 esperado)

| metrica | esperado |
|---------|---------:|
| total | 27 |
| sem_cnpj | 2 (CRO + VIA 040) |
| com_cnpj_valido | 25 (1 ECOVIAS original + 20 da manha + 4 agora) |
| com_cnpj_invalido | 0 |
| cnpj_status='validated_manual_brasilapi' | 4 |
| cnpj_status='validated_brasilapi' | 20 |
| cnpj_status='concessao_revogada' | 1 |
| cnpj_status='pending_investigation' | 1 |
| cnpj_status='ok' (ECOVIAS ARAGUAIA original) | 1 |
| cnpj_status='invalid_dv' restantes | 0 |

## Reverter (se necessario)

Antes do COMMIT: ROLLBACK em vez de COMMIT no arquivo `.sql` de commit.
Apos COMMIT: backup pre-mudanca em pg_dump nao foi tirado por essa onda
ser pequena (6 obras, valores pontuais); restauracao caso a caso via
UPDATE pontual referenciando manifesto pre-mudanca + Fase A3 anterior.
