FROM python:3.13-slim

# ffmpeg — Telegram отдаёт голосовые в ogg/opus и кружки в mp4,
# а Vosk читает только сырой PCM 16 кГц
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VOSK_MODEL=/models/vosk-model-small-ru-0.22

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

# модель не копируется в образ: она монтируется томом, иначе каждая
# пересборка тащила бы за собой 88 МБ, а смена модели требовала бы новый образ
EXPOSE 8080
CMD ["python", "server.py"]
