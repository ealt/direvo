FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git passwd \
    && rm -rf /var/lib/apt/lists/*

# Install direvo
WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY docs /app/docs
COPY src /app/src
RUN pip install --no-cache-dir '.[dev]'

# Copy entrypoint
COPY docker/entrypoint.sh /usr/local/bin/direvo-entrypoint
RUN chmod 0755 /usr/local/bin/direvo-entrypoint

# Copy experiment into the container
COPY example/data-fitting /experiment
WORKDIR /experiment

# Initialize the workspace git repo (can't live on the host because it would
# create a nested git repo inside the source tree)
RUN cd planner/workspace \
    && git init -q \
    && git config user.email "direvo@experiment" \
    && git config user.name "direvo" \
    && git add . \
    && git commit -q -m "initial baseline model"

ENTRYPOINT ["direvo-entrypoint"]
CMD ["run", "--config", "/experiment/.direvo/config.yaml"]
