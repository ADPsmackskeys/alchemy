# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py ./
COPY entitlement_catalog.json entitlement_risk_scores.json ./

# Writes replace a data file via a temp file in the same directory, so the
# directory itself must be writable — and bind-mounting a single file over
# either JSON would break that rename. To persist changes, mount a
# directory and point the app at it:
#   docker run -p 8001:8001 -v "$PWD/data:/data" \
#     -e ENTITLEMENT_CATALOG_FILE=/data/entitlement_catalog.json \
#     -e ENTITLEMENT_RISK_SCORES_FILE=/data/entitlement_risk_scores.json \
#     entitlements-api
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health').read()"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
