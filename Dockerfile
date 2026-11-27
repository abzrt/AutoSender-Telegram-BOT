FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Official Playwright image already includes Chromium & all OS/font dependencies!
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Salin kode bot, WFM standalone worker & jadwal awal
COPY bot.py config.py wfm_worker.py schedules.json ./

CMD ["python", "bot.py"]
