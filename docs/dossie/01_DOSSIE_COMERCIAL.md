# Dossiê comercial da obra

- Endpoint: GET /api/obras/{obra_id}/dossie (auth required).
- Serviço: app/services/obra_dossie.py
- UI: página individual da obra (app/frontend/app.html)
- Chave canônica de sede: localizacao.contratante (não contratante_sede)
- Somente obras status_portao=APROVADA
