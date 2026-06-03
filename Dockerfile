FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git + gh CLI + build tools (needed for some optional native deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# Install with webhook + dashboard + ollama (Anthropic/OpenAI optional, add at runtime)
RUN pip install -e ".[webhook,dashboard,ollama,anthropic,openai]"

ENV DEEPAGENT_WORKDIR=/var/lib/gh-deepagent
RUN mkdir -p "$DEEPAGENT_WORKDIR"

EXPOSE 8080
CMD ["gh-deepagent", "serve", "--host", "0.0.0.0", "--port", "8080"]
