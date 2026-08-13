# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py ./
COPY sod_rules.json .

# Writes replace the data file via a temp file in the same directory, so the
# directory itself must be writable — and bind-mounting a single file over
# sod_rules.json would break that rename. To persist changes, mount a
# directory and point the app at it:
#   docker run -p 8005:8005 -v "$PWD/data:/data" \
#     -e SOD_RULES_FILE=/data/sod_rules.json sod-rules-api
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/health').read()"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
