FROM python:3.11-slim

WORKDIR /app

# Dipendenze di sistema minime richieste da alcuni server MCP (build di eventuali wheel nativi).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Dati persistenti (config server, utenti, credenziali) -> montati come volume in produzione.
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

# Sessioni SSE e pool di processi MCP sono in memoria -> un solo worker gunicorn.
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "600", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
