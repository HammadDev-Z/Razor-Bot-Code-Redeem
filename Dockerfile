FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# tzdata: TZ (docker-compose.yml) shows local times in /status.
# xvfb: a virtual screen, so Chromium runs normally rather than headless.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium

RUN groupadd --system bot && useradd --system --gid bot --create-home bot
COPY --chown=bot:bot . .
# data/ holds the encrypted login, codes and order; debug/ holds snapshots.
RUN mkdir -p /app/data /app/debug && chown -R bot:bot /app/data /app/debug
USER bot

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["python", "bot.py"]
