FROM python:3.12-slim-bookworm

# Chromium + son driver (versions assorties) pour Selenium en headless
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium chromium-driver fonts-liberation ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/tmp/scraper-data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 1 seul worker (l'état des scripts en cours vit dans ce process) + plusieurs threads
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:${PORT:-10000} app:app"]
