# Runtime image for the Interdict MCP server (`interdict`).
#
#   docker build -t interdict .
#   docker run --rm -it --network=host interdict          # talk to a local DB
#   # or, via compose (brings up Postgres too):
#   docker compose --profile app run --rm app
FROM python:3.11-slim

WORKDIR /app

# gcc for building the asyncpg/pglast extensions on slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install just what the package needs (engine + adapters + bundled policies).
COPY pyproject.toml README.md ./
COPY engine ./engine
COPY adapters ./adapters
COPY policies ./policies
RUN pip install --no-cache-dir .

# Default to the compose Postgres host; override for any other database.
ENV AGENT_DB_DSN=postgresql://postgres:postgres@postgres:5432/pagila \
    AGENT_AUDIT_LOG=/app/logs/audit.jsonl

ENTRYPOINT ["interdict"]
