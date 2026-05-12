# Fase A3 — Repopulação de CNPJs antt_rod (manifesto pré-mudança)

**Data:** 2026-05-10
**Operador:** Claude Code (sessão williamvnvn@gmail.com)
**Autorização:** William, Caminho B (17 sucessos automáticos + 3 da revisão visualmente conferidos)
**Total a atualizar:** 20 obras

## Contexto

Em **09/05/2026 09:16-09:19 UTC**, sessão Claude Code anterior executou dois `UPDATE obras SET cnpj=NULL`
em massa, zerando CNPJs com DV inválido em todas as obras das 7 fontes "órfãs" (sem captador oficial).
A intenção era cleanup pra forçar reenriquecimento; o efeito foi remover CNPJs (alucinados originalmente
pelo dict `CONC_MAP` do `/tmp/captar_antt.py`) sem reposição.

Para `fonte='antt_rod'`: 27 obras totais, 26 ficaram com `cnpj IS NULL` e
`cnpj_status='invalid_dv'`. Apenas ECOVIAS DO ARAGUAIA sobreviveu (CNPJ originalmente correto).

Detalhes da investigação forense em `/tmp/claude-0/-root-wins-hub/.../tasks/`
(sessões da janela 09/05 → 10/05) e nos JSONs:

- `/tmp/antt_rod_cnpjs_recuperados.json` — 17 sucessos automáticos
- `/tmp/antt_rod_cnpjs_revisao.json` — 7 itens (3 conferidos manualmente: RIOSP, VIACOSTEIRA, MSVIA)
- `/tmp/antt_rod_cnpjs_falhas.json` — 2 falhas (busca DDG vazia)
- `/var/log/wins_hub/repopular_cnpj_antt_rod.log` — log da extração de 21.4 min

## Fonte do CNPJ

- **Primária:** DuckDuckGo HTML search por `"<sigla>" rodovia CNPJ`
- **Validação cruzada:** BrasilAPI `/api/cnpj/v1/<cnpj>` (oficial)
- **Critério matriz:** sufixo `/0001-` (descarta filiais)
- **Validações:** DV1+DV2 corretos via função canonica (mesma da função `cnpj_valido` do banco)
- **Auditoria humana:** 3 itens com score fuzzy < 0.6 mas razão social conferida visualmente

## 20 obras que receberão UPDATE

| # | id_externo | empresa | cnpj_novo | score | observação |
|--:|-----------|---------|-----------|------:|------------|
| 1 | ANTT-ROD-AUTOPISTA_LITORAL_SUL | AUTOPISTA LITORAL SUL S.A. | 09313969000197 | 1.00 | match exato |
| 2 | ANTT-ROD-VIA_ARAUCARIA | VIA ARAUCÁRIA - CONCESSIONÁRIA DE RODOVIAS S.A. | 47155252000153 | 1.00 | match exato |
| 3 | ANTT-ROD-ECOVIAS_DO_CERRADO | ECOVIAS DO CERRADO S.A. | 35593905000105 | 1.00 | match exato |
| 4 | ANTT-ROD-ECO050 | ECO050 - CONCESSIONÁRIA DA RODOVIA BR-050 S.A. | 19208022000170 | 0.80 | razão social bate |
| 5 | ANTT-ROD-ECO101_CONCESSIONARIA_DE_RODOVIAS_S/A | ECO101 CONCESSIONÁRIA DE RODOVIAS S/A | 15484093000144 | 1.00 | match exato |
| 6 | ANTT-ROD-AUTOPISTA_FERNAO_DIAS | AUTOPISTA FERNÃO DIAS S.A. | 09326342000170 | 1.00 | match exato |
| 7 | ANTT-ROD-LITORAL_PIONEIRO | LITORAL PIONEIRO - CONCESSIONÁRIA DE RODOVIAS S.A. | 51137031000120 | 0.89 | EPR Litoral Pioneiro (sucessora) |
| 8 | ANTT-ROD-AUTOPISTA_REGIS_BITTENCOURT | AUTOPISTA RÉGIS BITTENCOURT S.A. | 09336431000106 | 1.00 | match exato |
| 9 | ANTT-ROD-CONCEBRA | CONCEBRA - CONCESSIONÁRIA DAS RODOVIAS CENTRAIS DO BRASIL S.A. | 18572225000188 | 1.00 | match exato |
| 10 | ANTT-ROD-AUTOPISTA_PLANALTO_SUL | AUTOPISTA PLANALTO SUL S.A. | 09325109000173 | 1.00 | match exato |
| 11 | ANTT-ROD-AUTOPISTA_FLUMINENSE | AUTOPISTA FLUMINENSE S.A. | 09324949000111 | 1.00 | match exato |
| 12 | ANTT-ROD-TRANSBRASILIANA | TRANSBRASILIANA - CONCESSIONÁRIA DA RODOVIA BR-153 S.A. | 09074183000164 | 0.91 | razão social bate |
| 13 | ANTT-ROD-ECOSUL | ECOSUL - EMPRESA CONCESSIONÁRIA DE RODOVIAS DO SUL S.A. | 02511048000190 | 0.71 | razão social bate |
| 14 | ANTT-ROD-ECOPONTE | ECOPONTE - CONCESSIONÁRIA DA PONTE RIO-NITERÓI S.A. | 22163297000149 | 0.62 | razão social bate |
| 15 | ANTT-ROD-VIA_MINEIRA | VIA MINEIRA - CONCESSIONÁRIA DE RODOVIAS S.A. | 55231969000165 | 0.85 | EPR Via Mineira (sucessora) |
| 16 | ANTT-ROD-CONCER | CONCER - COMPANHIA DE CONCESSÃO RODOVIÁRIA JUIZ DE FORA-RIO S.A. | 00880446000158 | 0.92 | match razão social |
| 17 | ANTT-ROD-RODOVIA_DO_ACO | RODOVIA DO AÇO - CONCESSIONÁRIA DE RODOVIAS S.A. | 09414761000164 | 0.60 | K-Infra (sucessora) |
| 18 | ANTT-ROD-RIOSP | RIOSP - CONCESSIONÁRIA DA RODOVIA RIO-SANTOS S.A. | 44319688000142 | 0.56 | **conferido humano** — sigla acrônima |
| 19 | ANTT-ROD-VIACOSTEIRA | VIACOSTEIRA - CONCESSIONÁRIA DA RODOVIA SC-401 S.A. | 36763716000198 | 0.29 | **conferido humano** — Concessionária Catarinense |
| 20 | ANTT-ROD-MSVIA | MSVIA - CONCESSIONÁRIA DA RODOVIA MS-306/MS-040 S.A. | 19642306000170 | 0.24 | **conferido humano** — Concessionária Sul-Matogrossense |

Todos com `situacao_cadastral=ATIVA` e `cnae_fiscal=5221` (concessionárias rodoviárias).

## 6 obras pendentes (NÃO atualizadas nesta fase)

Permanecem com `cnpj IS NULL` e `cnpj_status='invalid_dv'` — tratamento separado:

| id_externo | empresa | motivo pendente |
|------------|---------|-----------------|
| ANTT-ROD-CRO | CRO - CONCESSIONÁRIA DA RODOVIA OSÓRIO-PORTO ALEGRE S.A. | DDG retornou homônimo (Conservação de Rodovias Oliveira) — busca manual |
| ANTT-ROD-VIA_SUL | VIA SUL - CONCESSIONÁRIA DE RODOVIAS S.A. | DDG retornou empresa de comércio de automóveis — busca manual |
| ANTT-ROD-VIABRASIL | VIABRASIL - CONCESSIONÁRIA DA BR-116/376/101 S.A. | DDG retornou fábrica de tintas — busca manual |
| ANTT-ROD-VIA_040 | VIA 040 - CONCESSIONÁRIA DO SISTEMA RODOVIÁRIO S.A. | CNPJ encontrado mas situação BAIXADA — concessão revogada, decidir |
| ANTT-ROD-ECO_RIOMINAS | ECO RIOMINAS S.A. | DDG sem hits — busca manual no Google |
| ANTT-ROD-VIA_BAHIA | VIA BAHIA - CONCESSIONÁRIA DA BAHIA S.A. | DDG sem hits — busca manual no Google |

## SQL exato (a ser executado dentro de transação BEGIN..COMMIT)

```sql
BEGIN;

-- (B3) SELECT before — estado atual
SELECT id_externo, LEFT(empresa,55) AS empresa, cnpj, cnpj_status, valor_estimado
  FROM obras
 WHERE fonte='antt_rod'
   AND id_externo IN (
     'ANTT-ROD-AUTOPISTA_LITORAL_SUL', 'ANTT-ROD-VIA_ARAUCARIA',
     'ANTT-ROD-ECOVIAS_DO_CERRADO', 'ANTT-ROD-ECO050',
     'ANTT-ROD-ECO101_CONCESSIONARIA_DE_RODOVIAS_S/A',
     'ANTT-ROD-AUTOPISTA_FERNAO_DIAS', 'ANTT-ROD-LITORAL_PIONEIRO',
     'ANTT-ROD-AUTOPISTA_REGIS_BITTENCOURT', 'ANTT-ROD-CONCEBRA',
     'ANTT-ROD-AUTOPISTA_PLANALTO_SUL', 'ANTT-ROD-AUTOPISTA_FLUMINENSE',
     'ANTT-ROD-TRANSBRASILIANA', 'ANTT-ROD-ECOSUL', 'ANTT-ROD-ECOPONTE',
     'ANTT-ROD-VIA_MINEIRA', 'ANTT-ROD-CONCER', 'ANTT-ROD-RODOVIA_DO_ACO',
     'ANTT-ROD-RIOSP', 'ANTT-ROD-VIACOSTEIRA', 'ANTT-ROD-MSVIA'
   )
 ORDER BY id_externo;

-- (B4) UPDATE — usa CASE pelo id_externo (chave única)
UPDATE obras
   SET cnpj = CASE id_externo
     WHEN 'ANTT-ROD-AUTOPISTA_LITORAL_SUL' THEN '09313969000197'
     WHEN 'ANTT-ROD-VIA_ARAUCARIA' THEN '47155252000153'
     WHEN 'ANTT-ROD-ECOVIAS_DO_CERRADO' THEN '35593905000105'
     WHEN 'ANTT-ROD-ECO050' THEN '19208022000170'
     WHEN 'ANTT-ROD-ECO101_CONCESSIONARIA_DE_RODOVIAS_S/A' THEN '15484093000144'
     WHEN 'ANTT-ROD-AUTOPISTA_FERNAO_DIAS' THEN '09326342000170'
     WHEN 'ANTT-ROD-LITORAL_PIONEIRO' THEN '51137031000120'
     WHEN 'ANTT-ROD-AUTOPISTA_REGIS_BITTENCOURT' THEN '09336431000106'
     WHEN 'ANTT-ROD-CONCEBRA' THEN '18572225000188'
     WHEN 'ANTT-ROD-AUTOPISTA_PLANALTO_SUL' THEN '09325109000173'
     WHEN 'ANTT-ROD-AUTOPISTA_FLUMINENSE' THEN '09324949000111'
     WHEN 'ANTT-ROD-TRANSBRASILIANA' THEN '09074183000164'
     WHEN 'ANTT-ROD-ECOSUL' THEN '02511048000190'
     WHEN 'ANTT-ROD-ECOPONTE' THEN '22163297000149'
     WHEN 'ANTT-ROD-VIA_MINEIRA' THEN '55231969000165'
     WHEN 'ANTT-ROD-CONCER' THEN '00880446000158'
     WHEN 'ANTT-ROD-RODOVIA_DO_ACO' THEN '09414761000164'
     WHEN 'ANTT-ROD-RIOSP' THEN '44319688000142'
     WHEN 'ANTT-ROD-VIACOSTEIRA' THEN '36763716000198'
     WHEN 'ANTT-ROD-MSVIA' THEN '19642306000170'
   END,
   cnpj_status = 'validated_brasilapi'
 WHERE fonte='antt_rod'
   AND id_externo IN (... mesma lista acima ...);

-- (B5) SELECT after — confirmar
SELECT id_externo, LEFT(empresa,55) AS empresa, cnpj, cnpj_status, valor_estimado
  FROM obras
 WHERE fonte='antt_rod'
   AND id_externo IN (... mesma lista acima ...)
 ORDER BY id_externo;

-- B6: aguarda autorização explícita.
-- Esta etapa: ROLLBACK (dry-run, banco intacto)
-- Próxima etapa: COMMIT
```

## Observações sobre schema

- ❌ Briefing pediu `cnpj_pendente_validacao = FALSE` — coluna **não existe** na tabela `obras`. Removida do UPDATE.
- ❌ Briefing pediu `updated_at = NOW()` — coluna **não existe**. Removida do UPDATE.
- ✅ Vai apenas em `cnpj` e `cnpj_status` (ambas existem).

## Resultado esperado

Antes:
- `antt_rod`: 27 obras | 1 com CNPJ válido (ECOVIAS DO ARAGUAIA) | 26 com cnpj=NULL

Depois:
- `antt_rod`: 27 obras | **21 com CNPJ válido** | **6 com cnpj=NULL** (pendentes manuais)
- `cnpj_status` atualizado para `'validated_brasilapi'` nas 20 que receberam UPDATE
