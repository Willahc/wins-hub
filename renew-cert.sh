#!/bin/bash
# Renova o certificado SSL e recarrega o nginx se foi renovado

cd /root/wins_hub

docker run --rm \
  -v /root/wins_hub/certbot/conf:/etc/letsencrypt \
  -v /root/wins_hub/certbot/www:/var/www/certbot \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet

# Recarrega o nginx pra pegar o cert novo (só funciona se o container tá rodando)
docker compose exec -T nginx nginx -s reload 2>/dev/null || true
