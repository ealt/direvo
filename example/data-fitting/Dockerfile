FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git passwd curl ca-certificates gnupg ripgrep bubblewrap patch \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS (for Codex CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude CLI
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude

# Codex CLI
RUN npm install -g @openai/codex

# Install eden
WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY docs /app/docs
COPY src /app/src
RUN pip install --no-cache-dir '.[dev]'

# Copy shared entrypoint
COPY docker/entrypoint.sh /usr/local/bin/eden-entrypoint
RUN chmod 0755 /usr/local/bin/eden-entrypoint

# Demo-specific entrypoint (propagates mounted auth to trial users)
COPY example/data-fitting/auth-setup.sh /usr/local/bin/demo-entrypoint
RUN chmod 0755 /usr/local/bin/demo-entrypoint

# Copy experiment into the container
COPY example/data-fitting /experiment
WORKDIR /experiment

# Ensure deterministic demo data exists even if the checkout omitted generated npz files.
RUN python3 generate_data.py

# Initialize the workspace git repo (can't live on the host because it would
# create a nested git repo inside the source tree)
RUN cd planner/workspace \
    && git init -q \
    && git config user.email "eden@experiment" \
    && git config user.name "eden" \
    && git add . \
    && git commit -q -m "initial baseline model"

ENTRYPOINT ["demo-entrypoint"]
CMD ["run", "--config", "/experiment/.eden/config.yaml"]
