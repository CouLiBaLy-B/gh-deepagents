# All-in-one HF Space — runs webhook + worker + Redis + Streamlit dashboard
# under one supervisord. Demo only; not for prod.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates redis-server supervisor curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY --chown=user pyproject.toml README.md ./
COPY --chown=user src ./src
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -e ".[dashboard,webhook,anthropic,openai]"

# Supervisord configuration (4 processes).
COPY --chown=user deploy/huggingface/all-in-one/supervisord.conf /etc/supervisord.conf
# Optional reverse-proxy script if we want both /webhook and dashboard on :7860.
COPY --chown=user deploy/huggingface/all-in-one/router.py /home/user/app/router.py

# Make HF state dir writable (everything fits in /home/user).
RUN mkdir -p /home/user/data /home/user/logs && chown -R user /home/user

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DEEPAGENT_REDIS_URL=redis://localhost:6379/0 \
    DEEPAGENT_API_URL=http://localhost:8080 \
    DEEPAGENT_WORKDIR=/home/user/data/work \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 7860
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
