# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py ./

# DATABASE_URL must resolve from inside the container: the alchemy-postgres
# container's default (127.0.0.1:5433) is the *host's* loopback, which is not
# reachable from here. Join the alchemy-postgres container's docker network
# and point at it by service name, e.g.:
#   docker run -p 8000:8000 --network alchemy_default \
#     -e DATABASE_URL=postgresql://alchemy:alchemy@alchemy-postgres:5432/alchemy requests-api
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
