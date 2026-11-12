FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TERMOKONTUR_DB=/data/termokontur.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin termokontur && \
    mkdir -p /data && chown termokontur:termokontur /data
COPY termokontur ./termokontur
USER termokontur
EXPOSE 8092
CMD ["uvicorn", "termokontur.app:app", "--host", "0.0.0.0", "--port", "8092"]
