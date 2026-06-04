# Hugging Face Spaces — Streamlit dashboard only.
# HF Spaces run containers as UID 1000 on port 7860.
FROM python:3.12-slim

# System deps: git is needed by GitPython for the rare in-process trigger path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create the user HF will switch to at runtime.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

# Install Python deps first (cache-friendly).
COPY --chown=user pyproject.toml README.md ./
COPY --chown=user src ./src
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -e ".[dashboard,webhook,anthropic,openai,ollama,google,groq]"

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONPATH=/home/user/app/src \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 7860

# HF routes public traffic to app_port (7860). The dashboard reads
# DEEPAGENT_API_URL from the env (set in Space Settings → Variables).
CMD ["streamlit", "run", "src/gh_deepagent/dashboard/app.py", \
     "--server.address=0.0.0.0", "--server.port=7860"]
