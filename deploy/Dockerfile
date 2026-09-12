FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The EGM dependency is pinned to a Git commit, so the image needs git only
# while installing. No credentials or application secrets are baked into it.
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY aftercare_agent ./aftercare_agent

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "aftercare_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
