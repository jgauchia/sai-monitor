FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAI_DB=/data/sai.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sai_monitor.py database.py webapp.py ./
COPY web ./web

RUN useradd --uid 1000 --user-group --create-home \
        --shell /usr/sbin/nologin sai \
    && mkdir -p /data \
    && chown -R sai:sai /app /data

USER sai

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8000"]
