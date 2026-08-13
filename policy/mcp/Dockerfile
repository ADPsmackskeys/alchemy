# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py ./

# This server holds no data of its own -- it is an HTTP client of the Policy
# API, so POLICY_API_URL must name a host this container can actually
# reach. The 127.0.0.1 default would resolve to this container itself:
#   docker network create policy
#   docker run -d --network policy --name api -p 8004:8004 policy-api
#   docker run -d --network policy --name mcp -p 8104:8104 \
#     -e POLICY_API_URL=http://api:8004 policy-mcp
#
# Register the running server with a client:
#   claude mcp add --transport http policy http://127.0.0.1:8104/mcp
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# 0.0.0.0 so the port is reachable from outside the container; config.py
# defaults to 127.0.0.1, which would only be reachable from within it.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8104

EXPOSE 8104

# /mcp expects a protocol handshake rather than a plain GET, so health is a
# TCP connect: the port is either accepting or it is not.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ['MCP_PORT'])), 2).close()"

CMD ["python", "main.py"]
