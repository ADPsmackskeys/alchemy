# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py agent.py main.py ./

# This agent holds no data of its own -- it is a client of the new-joiners MCP
# server, which is in turn a client of the API. MCP_URL must name a host this
# container can actually reach; the 127.0.0.1 default would resolve to this
# container itself:
#   docker network create new-joiners
#   docker run -d --network new-joiners --name api -p 8000:8000 new-joiners-api
#   docker run -d --network new-joiners --name mcp -p 8100:8100 \
#     -e NEW_JOINERS_API_URL=http://api:8000 new-joiners-mcp
#   docker run -d --network new-joiners --name agent -p 8200:8200 \
#     -e MCP_URL=http://mcp:8100/mcp -e GEMINI_API_KEY=... new-joiners-agent
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# 0.0.0.0 so the port is reachable from outside the container; config.py
# defaults to 127.0.0.1, which would only be reachable from within it.
ENV HOST=0.0.0.0 \
    PORT=8200

EXPOSE 8200

# /health answers before the model or MCP server are reachable, reporting
# "degraded" rather than failing, so this checks the process is serving.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\").read()"

CMD ["python", "main.py"]
