# Образ приложения «Кабинет заданий». Собирается из официального python:3.12-slim,
# ничего не компилируется на месте: все зависимости — готовые wheel-пакеты.
#
#   docker build -t zadanie .
#   docker run -d --name zadanie -p 8000:8000 -v zadanie-data:/data zadanie

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data \
    APP_PORT=8000

WORKDIR /app

# зависимости отдельным слоем: пересобираются только при смене requirements.txt
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# код: сервер, вопросы, вспомогательные скрипты и статика
COPY main.py admin_auth.py paths.py questions.py results.py admin_passwd.py ./
COPY static ./static

# отдельный непривилегированный пользователь и том для данных
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${APP_PORT}/api/health', timeout=2).status == 200 else 1)"

# ВАЖНО: рабочий процесс один. Счётчики защиты (лимит отправок, блокировка входа
# в админку) живут в памяти процесса, поэтому несколько воркеров или реплик
# ослабили бы защиту. Нагрузка тут маленькая, одного процесса достаточно.
#
# --proxy-headers нужен, если приложение стоит за nginx/Caddy: только тогда
# схема запроса (http/https) и IP клиента берутся из X-Forwarded-*.
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port ${APP_PORT} --no-server-header --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-*}"]
