FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git passwd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY docs /app/docs
COPY src /app/src
RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /usr/local/bin/direvo-entrypoint
RUN chmod 0755 /usr/local/bin/direvo-entrypoint

WORKDIR /workspace
ENTRYPOINT ["direvo-entrypoint"]
