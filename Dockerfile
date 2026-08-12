# bucker-agent runtime image (API + worker). The sandbox runs as a
# separate image (Dockerfile.sandbox) driven over the docker socket.
# Digest pinned 2026-08-05 (python:3.12-slim) — matches Dockerfile.sandbox.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# docker CLI: the worker launches sandbox containers via the host socket
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates docker.io \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package itself (full = Temporal+Postgres, llm = provider SDKs) + uvicorn
COPY pyproject.toml uv.lock README.md ./
COPY bucker ./bucker
RUN pip install --no-cache-dir ".[full,llm]" uvicorn

ENV BUCKER_BLOB_ROOT=/data/blobs \
    BUCKER_MEMORY_ROOT=/data/memory \
    BUCKER_SKILLS_ROOT=/data/skills \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

EXPOSE 8123

# Default = the API; the worker service overrides the command.
CMD ["python", "-m", "uvicorn", "bucker.api.app:app", "--host", "0.0.0.0", "--port", "8123"]
