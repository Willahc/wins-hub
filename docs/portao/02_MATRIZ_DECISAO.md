# Matriz de decisão — Portão v5

## APROVADA (automática)
- Intervenção física explícita
- Ativo físico identificável
- Setor elegível
- Valor ≥ R$ 100.000 associado ao empreendimento
- Fonte/evidência rastreável

## REJEITADA (automática)
- Não-obra clara (software, consultoria, material sem instalação, etc.)
- Manutenção rotineira sem obra estrutural
- Valor < R$ 100.000
- Duplicata de empreendimento

## EM_ANALISE
- Serviço de engenharia genérico
- Notícia sem evidência suficiente
- Valor ausente
- Critérios parciais (possibilidade de obra sem prova plena)

## ERRO_PORTAO
- Falha técnica após max tentativas na fila
- visivel=false (não é rejeição de mérito)

## Pipeline comercial
- Somente obra APROVADA com enriquecimento insuficiente
- Não significa dúvida sobre ser obra
