FROM python:3.13-slim

# tini — чтобы docker stop / Ctrl+C доходили до python как обычный SIGTERM
# и бот завершался штатно (закрыл базу, отключил юзербота), а не убивался
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# cloudflared: бот сам поднимает им быстрый туннель для веб-панели.
# Аккаунт и токен не нужны, адрес выдаётся на лету. Не нужен туннель —
# TUNNEL_ON=0, бинарник просто не запускается.
ARG TARGETARCH=amd64
ADD https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH} /usr/local/bin/cloudflared
RUN chmod 0755 /usr/local/bin/cloudflared

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

# зависимости отдельным слоем: правки кода не тянут переустановку пакетов
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gremlin ./gremlin

# всё изменяемое (база, сессия, медиа триггеров, логи) живёт в /app/data —
# он монтируется с хоста, поэтому переживает пересборку образа
RUN useradd -u 1000 -m gremlin \
 && mkdir -p /app/data \
 && chown -R gremlin:gremlin /app
USER gremlin

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "gremlin"]
