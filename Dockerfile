FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# BEGIN GENERATED: packages (python scripts/sync_intents.py; do not edit)
COPY telegraphintent ./telegraphintent
COPY fraudrisk ./fraudrisk
COPY factcheck ./factcheck
COPY audit ./audit
COPY ipgeo ./ipgeo
COPY emailsec ./emailsec
COPY urlsafe ./urlsafe
COPY malware ./malware
COPY intents ./intents
COPY intentkit ./intentkit
# END GENERATED: packages
COPY miner.yaml ./miner.yaml

RUN useradd -m app && chown -R app /app
USER app

EXPOSE 8000
# $PORT is injected by Render/Railway/Fly; default 8000 locally.
# One worker keeps each intent's in-memory cache (and the fraud bulk feeds) coherent.
CMD ["sh", "-c", "uvicorn telegraphintent.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 30"]
