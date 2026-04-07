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

# Install canonical entrypoint and scripts from package
COPY src/eden/docker/entrypoint.sh /usr/local/bin/eden-container-entrypoint
RUN chmod 0755 /usr/local/bin/eden-container-entrypoint
COPY src/eden/docker/auth-setup.sh /usr/local/bin/eden-auth-setup
RUN chmod 0755 /usr/local/bin/eden-auth-setup
COPY src/eden/docker/export.sh /usr/local/bin/eden-export
RUN chmod 0755 /usr/local/bin/eden-export

# Backward-compat wrapper entrypoint
COPY docker/entrypoint.sh /usr/local/bin/eden-entrypoint
RUN chmod 0755 /usr/local/bin/eden-entrypoint

WORKDIR /workspace
ENTRYPOINT ["eden-entrypoint"]
