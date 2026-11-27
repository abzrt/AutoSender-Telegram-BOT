FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependensi & Playwright Chromium terlebih dahulu agar Docker layer cache optimal
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt && playwright install --with-deps chromium

# Salin kode bot, WFM standalone worker & jadwal awal
COPY bot.py config.py wfm_worker.py schedules.json ./

CMD ["python", "bot.py"]
