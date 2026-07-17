# Fluxo definitivo — Portão de Obras

```
CAPTURA BRUTA (captadores inalterados)
  → INSERT public.obras
  → se flags NEW_CAPTURES: status_portao=EM_ANALISE, visivel=false
  → wins_v2.portao_fila
  → processar_fila_portao / processar_fila()
  → REJEITADA | EM_ANALISE | APROVADA
  → se APROVADA: enrichment_queue + status_enriquecimento=EM_PROCESSAMENTO
  → classificação comercial (OURO/PRATA/BRONZE/PIPELINE) após enriquecimento
  → site só lista status_portao IS NULL (histórico) OR APROVADA
```

## Feature flags (`wins_v2.portao_config`)

| Flag | Fase atual |
|---|---|
| PORTAO_OBRAS_ENABLED | true |
| PORTAO_OBRAS_NEW_CAPTURES_ENABLED | true |
| AUTO_ENRICH_AFTER_GATE_ENABLED | true |
| PORTAO_OBRAS_HISTORICAL_ENABLED | false |
| PORTAO_OBRAS_AGENT_ENABLED | false |

Histórico **não** é reclassificado em massa até amostragem aprovada.
