FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    DB_PATH=data/aggregator.db \
    LOG_FILE=logs/collector.log

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при изменении requirements
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data logs

# Сервисный режим: прогон при старте, cron, бот в одном процессе
CMD ["python", "main.py"]
