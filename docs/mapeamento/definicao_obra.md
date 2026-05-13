# Definição formal de "Obra" (WiNS Hub)

> Documento normativo: o que entra na base, o que vira **Ouro** elegível e o que está fora de escopo.
> Versão 1.0 · 2026-05-13

## 1. Critério mínimo de CAPEX

Tudo é avaliado em **R$ correntes do ano-base da notícia** (sem ajuste IPCA — o filtro é grosseiro).

| Tier        | CAPEX mínimo | Tratamento                                                        |
| ----------- | -----------: | ----------------------------------------------------------------- |
| **Pipeline** |    R$ 10 M   | Entra na base. Visível pra clientes Standard. Não vira matchmaking Premium. |
| **Prata**    |    R$ 50 M   | Visível Premium. Score CNAE só. Sem decisor obrigatório.          |
| **Ouro**     |   R$ 100 M   | Ouro elegível **se** tiver decisor (nome+cargo+email/LinkedIn).   |
| **Mega-obra**|   R$ 1 B     | Destaque dashboard + alerta realtime CNAE.                        |

Se a notícia não trouxer CAPEX explícito, aplicar **proxy por tipo**:

- Fábrica anunciada com >50 empregos diretos → presumir R$ 30 M+
- Subestação/linha de transmissão 138-230 kV → R$ 30-100 M
- Subestação 500 kV → R$ 200 M+
- Hidrelétrica/eólica/solar com MW declarado → R$ 4 M/MW (eólica/solar), R$ 6 M/MW (hidro)
- Hospital >100 leitos → R$ 150 M
- Trecho ferroviário/rodoviário/m → R$ 8 M
- Data center MW IT → R$ 50 M/MW

Sem CAPEX explícito **e** sem proxy aplicável → `confianca_extracao=0.4`, status `rumor`, NÃO sobe pra Pipeline até validação humana.

## 2. Tipos cobertos

Em ordem de prioridade comercial atual (julho/2026):

1. **Industrial** — fábricas novas, expansão de planta, retrofit, modernização linhas.
2. **Infraestrutura linear** — rodovia, ferrovia, hidrovia, porto, aeroporto, terminais.
3. **Energia** — geração (hidro, eólica, solar, térmica, nuclear), transmissão, subestação, distribuição.
4. **Mineração** — abertura de mina, beneficiamento, pilha de estéril, barragem, expansão.
5. **Logística** — CDs >20 mil m², terminais intermodais, hub e-commerce, cold storage.
6. **Saneamento** — ETA, ETE, adutora >10 km, emissário submarino, coleta de esgoto PPP.
7. **Saúde** — hospital novo >100 leitos, UPA, CER, hospital especializado, expansão >R$ 50 M.
8. **Educação** — universidade campus novo, escola técnica federal, FNDE escolas >R$ 30 M lote.
9. **Defesa** — fragatas, submarinos, blindados, aeronaves, modernização base, contratos OPV/CG.
10. **Agro-industrial** — usina sucroenergética, frigorífico, esmagadora soja, laticínio grande porte.
11. **Data Center** — colocation, hyperscale, edge >5 MW IT.
12. **Imobiliário industrial** — galpão modular spec/built-to-suit >30 mil m², condomínio logístico.

> **Saúde / Educação / Defesa / Imobiliário industrial** são tipos **novos** ainda não tocados pelo pipeline atual. Documento de mapeamento (B2) deve cobrir fontes para acomodá-los.

## 3. Status workflow (fase comercial)

```
rumor → anunciado → licenciado → contratado → em_obra → operacional
                ↓                       ↓
              cancelado              adiado
```

| Status        | Descrição                                                                                | Visibilidade |
| ------------- | ---------------------------------------------------------------------------------------- | ------------ |
| `rumor`       | Mencionado em notícia mas sem confirmação oficial nem CAPEX validado.                    | Premium      |
| `anunciado`   | Empresa, governo ou regulador comunica formalmente. CAPEX e prazo declarados.            | Standard+    |
| `licenciado`  | IBAMA/CETESB/Inea/etc emitiram LP/LI/LO. Decisão de investimento confirmada.             | Standard+    |
| `contratado`  | EPC/EPCM assinado, ordem de compra emitida, edital homologado, ata de RP publicada.      | Standard+    |
| `em_obra`     | Canteiro instalado, obra civil em curso. Demandas de fornecimento "agora".               | Standard+    |
| `operacional` | Comissionamento concluído. Sai do pipeline de oportunidade (vira intel de mercado).      | Premium      |
| `cancelado`   | Empresa desistiu, prazo expirado, edital deserto, financiamento negado.                  | Histórico    |
| `adiado`      | Anunciado mas com prazo de início >24 meses. Volta a `anunciado` quando aproxima.       | Standard+    |

Status `em_obra` e `contratado` são os **dois mais valiosos** para fornecedores de equipamentos/serviços — vender hoje, não daqui a 3 anos.

## 4. Geografia

Território nacional brasileiro. Inclui:

- 27 UFs (incluindo DF)
- Plataforma continental (offshore O&G, eólica offshore)
- Zonas econômicas especiais (ZEE, ZPE Pecém, Suape, Manaus)

Inclui obras de empresas brasileiras no exterior **só se** o EPC for brasileiro e CAPEX >R$ 500 M (ex: Odebrecht/Andrade/Camargo internacional histórico — hoje raríssimo).

## 5. Exclusões explícitas

Fora de escopo (não captar, descartar se aparecer):

- **Imóvel residencial** — incorporação habitacional, MCMV, vertical luxo. (Mesmo se R$ 100 M+ em obra civil.)
- **Varejo simples** — loja conceito, dark store, showroom <R$ 30 M.
- **Capital de giro / aquisição de empresa** — fusão, IPO, follow-on, financiamento BNDES sem destinação a CAPEX físico identificável.
- **Investimento financeiro** — aporte em fundo, debênture sem destinação a obra específica, FIDC.
- **Reforma simples** — pintura, troca de equipamento manutenção, retrofit <10% do valor original.
- **Pesquisa pura** — convênio universidade sem obra física.
- **Frota** — compra de caminhões/aeronaves/navios sem instalação fixa associada.

**Exceções** (entram apesar de aparecer como exclusão):

- Aquisição que **inclui** compromisso de CAPEX físico ≥R$ 50 M (ex: Petrobras compra refinaria e anuncia modernização) → entra como `anunciado` com o CAPEX da modernização.
- Centro de pesquisa **com obra civil** declarada (ex: Bosch P&D R$ 30 M com nova planta-piloto) → entra.

## 6. Confiança da extração

`confianca_extracao` ∈ [0.0, 1.0] por obra, vindo da Camada 5 (Haiku) ou do captador OFICIAL (sempre 1.0). Threshold:

- ≥ 0.8 → entra direto na base.
- 0.6-0.8 → entra com `status='rumor'`, exige confirmação humana ou segunda fonte.
- < 0.6 → descarta (`motivo_skip='confianca_baixa'`).

## 7. Decisor obrigatório para Ouro

Uma obra só é **Ouro elegível** se tiver pelo menos um decisor:

- `nivel1_nome` não nulo
- `nivel1_cargo` aprovado por `cargo_decisor_keyword()` (diretor, gerente sênior, VP, CEO, presidente, head, líder, coordenador de projeto/EPC/obras)
- pelo menos um de: `nivel1_email` (com domínio corporativo) **ou** `nivel1_linkedin`
- `fonte_tipo != 'NOTICIA'` (notícia só vira Ouro após enriquecimento P3.1 confirmar decisor em fonte oficial)

Obras Premium sem decisor caem em Prata. Conversão Prata → Ouro é onde o pipeline P3.1 (BrasilAPI + Hunter + busca LinkedIn) atua.
