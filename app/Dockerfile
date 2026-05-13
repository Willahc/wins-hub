FROM python:3.12-slim
WORKDIR /app

# Tools de build + libpq pra psycopg2 + apt deps pra Playwright chromium.
# Lista derivada de `playwright install --with-deps` (Debian 13 trixie),
# omitindo ttf-unifont/ttf-ubuntu-font-family (nao existem em trixie).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl libpq-dev gcc \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        libatspi2.0-0 libwayland-client0 \
        fonts-liberation fonts-freefont-ttf fonts-ipafont-gothic \
        fonts-wqy-zenhei fonts-unifont \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright chromium binary (system deps ja instalados acima).
# Sprint dia 3 (2026-05-13): bakear no image pra sobreviver a
# `docker compose up --force-recreate` (recreate reseta writable layer).
RUN playwright install chromium \
 && rm -rf /tmp/* /var/tmp/*

COPY . .
RUN mkdir -p frontend
EXPOSE 8000
CMD ["python", "main.py"]
