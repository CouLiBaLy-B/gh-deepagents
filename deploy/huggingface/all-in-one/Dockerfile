# All-in-one HF Space — runs webhook + worker + Redis + Streamlit dashboard
# under one supervisord. Demo only; not for prod.
#
# Public traffic on :7860 → Streamlit (the only HF-exposed port).
# Internal:  Streamlit ─HTTP─▶ webhook (127.0.0.1:8080) ─Redis─▶ worker
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

COPY --chown=user deploy/huggingface/all-in-one/supervisord.conf /etc/supervisord.conf

# State + log dirs (writable by UID 1000).
RUN mkdir -p /home/user/data /home/user/logs && chown -R user /home/user

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONPATH=/home/user/app/src \
    PYTHONUNBUFFERED=1 \
    DEEPAGENT_REDIS_URL=redis://localhost:6379/0 \
    DEEPAGENT_API_URL=http://127.0.0.1:8080 \
    DEEPAGENT_WORKDIR=/home/user/data/work \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 7860
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
