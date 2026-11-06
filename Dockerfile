FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# tzdata lets TZ (set in docker-compose.yml) show local times in /status.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium

RUN groupadd --system bot && useradd --system --gid bot --create-home bot
COPY --chown=bot:bot . .
# data/ holds the encrypted login, codes and order; debug/ holds snapshots.
RUN mkdir -p /app/data /app/debug && chown -R bot:bot /app/data /app/debug
USER bot

CMD ["python", "bot.py"]
